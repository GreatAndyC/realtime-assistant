"""Local, per-meeting speaker labels based on voice embeddings.

Embeddings remain in memory for the current WebSocket session. They are not
persisted or transmitted; only anonymous speaker labels enter the transcript.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import numpy as np


class VoiceEmbedder:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speaker-encoder")
        self._encoder = None

    async def embed(self, pcm: bytes) -> np.ndarray | None:
        if len(pcm) < 16000 * 2 or len(pcm) % 2:
            return None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._embed_sync, pcm)

    def _embed_sync(self, pcm: bytes) -> np.ndarray | None:
        from resemblyzer import VoiceEncoder, preprocess_wav

        wav = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        if not np.isfinite(wav).all() or float(np.sqrt(np.mean(wav * wav))) < 0.005:
            return None
        prepared = preprocess_wav(wav, source_sr=16000)
        if len(prepared) < 8000:
            return None
        if self._encoder is None:
            self._encoder = VoiceEncoder(device="cpu", verbose=False)
        vector = self._encoder.embed_utterance(prepared).astype(np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm and np.isfinite(norm) else None


@lru_cache(maxsize=1)
def get_embedder() -> VoiceEmbedder:
    return VoiceEmbedder()


class SpeakerDiarizer:
    """Assign stable anonymous labels to completed speech segments.

    A new label is created when its cosine similarity to every known voice is
    below the conservative threshold. Short segments inherit the most recent
    label; overlapping voices cannot be distinguished within one VAD segment.
    """

    def __init__(self, embedder: VoiceEmbedder | None = None, threshold: float = 0.82,
                 start_index: int = 0):
        self.embedder = embedder or get_embedder()
        self.threshold = threshold
        self.start_index = start_index
        self._prototypes: list[np.ndarray] = []
        self._last_label: str | None = None

    async def identify(self, pcm: bytes) -> str:
        vector = await self.embedder.embed(pcm)
        if vector is None:
            return self._last_label or "待确认发言人"
        if self._prototypes:
            scores = [float(np.dot(vector, prototype)) for prototype in self._prototypes]
            best = int(np.argmax(scores))
            if scores[best] >= self.threshold:
                combined = 0.75 * self._prototypes[best] + 0.25 * vector
                self._prototypes[best] = combined / np.linalg.norm(combined)
                self._last_label = f"说话人{self.start_index + best + 1}"
                return self._last_label
        self._prototypes.append(vector)
        self._last_label = f"说话人{self.start_index + len(self._prototypes)}"
        return self._last_label
