"""Anonymous speaker labels remain stable across alternating voices."""
import asyncio

import numpy as np

from src.speakers import SpeakerDiarizer


class FakeEmbedder:
    def __init__(self):
        self.vectors = iter([
            np.array([1.0, 0.0], dtype=np.float32),
            np.array([0.0, 1.0], dtype=np.float32),
            np.array([0.98, 0.02], dtype=np.float32),
            np.array([0.03, 0.97], dtype=np.float32),
        ])

    async def embed(self, pcm):
        return next(self.vectors)


def test_alternating_voices_keep_their_labels():
    diarizer = SpeakerDiarizer(embedder=FakeEmbedder())

    async def classify():
        return [await diarizer.identify(b"synthetic") for _ in range(4)]

    assert asyncio.run(classify()) == ["说话人1", "说话人2", "说话人1", "说话人2"]


def test_reconnected_session_uses_new_speaker_number():
    diarizer = SpeakerDiarizer(embedder=FakeEmbedder(), start_index=2)
    assert asyncio.run(diarizer.identify(b"synthetic")) == "说话人3"
