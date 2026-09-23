"""16 kHz PCM16 energy VAD and serialized, resident Whisper transcription.

Each WebSocket connection should own one ``EnergyVAD``. The application can
share one ``WhisperASR`` across connections; inference runs on its dedicated
worker thread so the event loop keeps receiving audio.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

from src.config import Config, config
from src.models import AudioSegment, Language


class EnergyVAD:
    """Split a PCM stream into utterances using 20 ms RMS frames.

    The threshold is relative to full scale (1.0). This deliberately simple
    VAD is most suitable for a quiet microphone; speech in noise may need a
    trained VAD. ``partial_snapshot`` copies the active utterance without
    consuming it. A later final result supersedes its partials.
    """

    def __init__(self, settings: Config = config, partial_interval_ms: int = 2000) -> None:
        if settings.sample_rate != 16000 or settings.channels != 1 or settings.sample_width != 2:
            raise ValueError("EnergyVAD requires mono 16 kHz PCM16")
        self.settings = settings
        self._frame_bytes = settings.sample_rate * settings.sample_width // 50
        self._silence_samples = settings.vad_silence_ms * settings.sample_rate // 1000
        self._max_samples = settings.vad_max_segment_ms * settings.sample_rate // 1000
        self._min_samples = settings.vad_min_segment_ms * settings.sample_rate // 1000
        self._partial_samples = partial_interval_ms * settings.sample_rate // 1000
        self._pending = bytearray()
        self._pending_seq = 0
        self._active = bytearray()
        self._meeting_id: Optional[str] = None
        self._speaker = ""
        self._language = Language.AUTO
        self._seq_start = 0
        self._seq_end = 0
        self._speech_samples = 0
        self._trailing_silence_samples = 0
        self._last_partial_speech_samples = 0

    def push(
        self, pcm_bytes: bytes, meeting_id: str, speaker: str,
        language: Language, seq: int,
    ) -> list[AudioSegment]:
        """Consume audio in order and return zero or more completed segments."""
        if len(pcm_bytes) % 2:
            raise ValueError("PCM16 frame has an odd byte count")
        if not pcm_bytes:
            return []
        completed: list[AudioSegment] = []
        language = Language(language)
        if self._meeting_id is not None and (
            meeting_id != self._meeting_id
            or speaker != self._speaker
            or language != self._language
        ):
            completed.extend(self.flush())
        if not self._pending:
            self._pending_seq = seq
        self._pending.extend(pcm_bytes)
        while len(self._pending) >= self._frame_bytes:
            frame = bytes(self._pending[:self._frame_bytes])
            del self._pending[:self._frame_bytes]
            completed.extend(self._consume_frame(frame, meeting_id, speaker, language, self._pending_seq))
            # A frame that spans packets retains its first packet's sequence;
            # subsequent complete frames start in this packet.
            self._pending_seq = seq
        return completed

    def partial_due(self) -> bool:
        """Whether a new partial subtitle is due for the active utterance."""
        if not self._active or self._meeting_id is None:
            return False
        if self._speech_samples < self._min_samples:
            return False
        if self._speech_samples - self._last_partial_speech_samples < self._partial_samples:
            return False
        return True

    def partial_snapshot(self) -> Optional[AudioSegment]:
        """Copy current speech for recognition, advancing the partial timer."""
        if not self.partial_due():
            return None
        self._last_partial_speech_samples = self._speech_samples
        pcm = bytes(self._active)
        if self._trailing_silence_samples:
            pcm = pcm[: -self._trailing_silence_samples * self.settings.sample_width]
        return self._segment(pcm)

    def flush(self) -> list[AudioSegment]:
        """Finish pending audio, including the last sub-20 ms frame."""
        completed: list[AudioSegment] = []
        if self._pending:
            frame = bytes(self._pending)
            self._pending.clear()
            if self._meeting_id is not None:
                completed.extend(self._consume_frame(
                    frame, self._meeting_id, self._speaker, self._language, self._pending_seq,
                ))
            # No active utterance means the pending bytes were silence or the
            # first (very short) frame. They cannot be a useful transcription.
        segment = self._finish()
        if segment is not None:
            completed.append(segment)
        return completed

    def _consume_frame(
        self, frame: bytes, meeting_id: str, speaker: str,
        language: Language, seq: int,
    ) -> list[AudioSegment]:
        samples = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        voiced = rms >= self.settings.vad_energy_threshold
        frame_samples = len(frame) // self.settings.sample_width
        if not self._active:
            if not voiced:
                return []
            self._meeting_id = meeting_id
            self._speaker = speaker
            self._language = language
            self._seq_start = seq
            self._last_partial_speech_samples = 0
        self._active.extend(frame)
        self._seq_end = seq
        if voiced:
            self._speech_samples += frame_samples
            self._trailing_silence_samples = 0
        else:
            self._trailing_silence_samples += frame_samples
        if self._trailing_silence_samples >= self._silence_samples:
            segment = self._finish()
            return [segment] if segment is not None else []
        if len(self._active) // self.settings.sample_width >= self._max_samples:
            segment = self._finish()
            return [segment] if segment is not None else []
        return []

    def _segment(self, pcm: bytes) -> AudioSegment:
        assert self._meeting_id is not None
        return AudioSegment(
            meeting_id=self._meeting_id,
            speaker=self._speaker,
            language=self._language,
            seq_start=self._seq_start,
            seq_end=self._seq_end,
            pcm_bytes=pcm,
        )

    def _finish(self) -> Optional[AudioSegment]:
        result = None
        if self._active and self._speech_samples >= self._min_samples:
            # Silence is useful for VAD but need not be decoded by Whisper.
            cut = self._trailing_silence_samples * self.settings.sample_width
            result = self._segment(bytes(self._active[:-cut] if cut else self._active))
        self._active.clear()
        self._meeting_id = None
        self._speech_samples = 0
        self._trailing_silence_samples = 0
        self._last_partial_speech_samples = 0
        return result


class WhisperASR:
    """Load pywhispercpp once and serialize all inference on one worker."""

    def __init__(self, model_path: Optional[str] = None) -> None:
        self.model_path = model_path if model_path is not None else config.whisper_model_path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper-asr")
        self._model = None

    async def prepare(self) -> None:
        """Eagerly load the model without blocking the event loop."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, self._load_model)

    async def transcribe(self, segment: AudioSegment) -> str:
        """Recognize one final or partial snapshot; return plain subtitle text."""
        if not segment.pcm_bytes:
            return ""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._transcribe_partial_sync, segment)

    async def transcribe_with_language(self, segment: AudioSegment) -> tuple[str, Language]:
        """Recognize speech and return the language used for this segment."""
        if not segment.pcm_bytes:
            return "", Language.ZH
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._transcribe_sync, segment)

    def _load_model(self):
        if self._model is None:
            if not self.model_path:
                raise RuntimeError("WHISPER_MODEL_PATH is required for local ASR")
            model_file = Path(self.model_path).expanduser()
            if not model_file.is_file():
                raise FileNotFoundError(f"Whisper model not found: {model_file}")
            from pywhispercpp.model import Model

            self._model = Model(str(model_file), print_realtime=False, print_progress=False)
        return self._model

    def _transcribe_sync(self, segment: AudioSegment) -> tuple[str, Language]:
        model = self._load_model()
        samples = np.frombuffer(segment.pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
        if segment.language != Language.AUTO:
            parts = model.transcribe(samples, language=segment.language.value, no_context=True)
            return "".join(part.text for part in parts).strip(), segment.language

        # Whisper's language detector often labels Cantonese as zh. Decode both
        # supported languages and compare the resulting token confidence.
        candidates = []
        for language in (Language.ZH, Language.YUE):
            parts = model.transcribe(samples, language=language.value,
                                     no_context=True, extract_probability=True)
            text = "".join(part.text for part in parts).strip()
            weight = sum(len(part.text.strip()) for part in parts)
            confidence = (sum(float(part.probability or 0) * len(part.text.strip())
                              for part in parts) / weight) if weight else 0.0
            candidates.append((confidence, text, language))
        _, text, language = max(candidates, key=lambda item: item[0])
        return text, language

    def _transcribe_partial_sync(self, segment: AudioSegment) -> str:
        model = self._load_model()
        samples = np.frombuffer(segment.pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
        language = "" if segment.language == Language.AUTO else segment.language.value
        parts = model.transcribe(samples, language=language, no_context=True)
        return "".join(part.text for part in parts).strip()


@lru_cache(maxsize=1)
def get_asr() -> WhisperASR:
    """Process-wide shared ASR instance for the server."""
    return WhisperASR()
