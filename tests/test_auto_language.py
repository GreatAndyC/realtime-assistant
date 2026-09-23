"""Automatic language selection runs for each utterance, not each meeting."""
import numpy as np

from src.asr import WhisperASR
from src.models import AudioSegment, Language


class FakeModel:
    def __init__(self):
        self.decoded_languages = []

    def transcribe(self, samples, *, language, no_context, extract_probability=False):
        self.decoded_languages.append(language)
        preferred = "zh" if samples[0] > 0 else "yue"
        probability = 0.9 if language == preferred else 0.6
        return [type("Part", (), {"text": "测试", "probability": probability})()]


def test_auto_language_is_selected_for_each_segment(monkeypatch):
    asr = WhisperASR()
    model = FakeModel()
    monkeypatch.setattr(asr, "_load_model", lambda: model)
    try:
        for sample, expected in ((10000, Language.ZH), (-10000, Language.YUE)):
            segment = AudioSegment(
                meeting_id="meeting", speaker="测试员", language=Language.AUTO,
                seq_start=0, seq_end=0,
                pcm_bytes=np.array([sample] * 16000, dtype="<i2").tobytes(),
            )
            assert asr._transcribe_sync(segment) == ("测试", expected)
        assert model.decoded_languages == ["zh", "yue", "zh", "yue"]
    finally:
        asr._executor.shutdown(wait=True)
