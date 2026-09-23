"""End-to-end WebSocket framing and meeting persistence without external models."""
from __future__ import annotations

import struct
import uuid

from fastapi.testclient import TestClient

from src.models import Language
from src.server import app
from src.storage import Storage


class FakeASR:
    async def transcribe(self, segment):
        return "今天决定采用方案A"


def test_websocket_audio_to_final_minutes(monkeypatch, tmp_path):
    import src.asr as asr
    import src.server as server

    meeting_id = f"test-{uuid.uuid4().hex}"
    test_storage = Storage(tmp_path / "meetings.db", tmp_path)
    monkeypatch.setattr(server, "storage", test_storage)
    monkeypatch.setattr(asr, "get_asr", lambda: FakeASR())
    events = []

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/meetings/{meeting_id}") as ws:
            ws.send_json({"type": "config", "language": "zh", "speaker": "测试员"})
            for seq in range(60):
                pcm = struct.pack("<320h", *([10000] * 320 if seq < 30 else [0] * 320))
                ws.send_bytes(struct.pack("<IQ", seq, seq * 320) + pcm)
            for _ in range(70):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "asr.final":
                    break
            ws.send_json({"type": "end_meeting"})
            for _ in range(100):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "agent.state" and event["data"]["state"] == "ENDED":
                    break

    assert any(e["type"] == "asr.final" and e["data"]["text"] == "今天决定采用方案A" for e in events)
    assert any(e["type"] == "minutes.ready" and e["data"]["phase"] == "final" for e in events)
    assert test_storage.count_utterances(meeting_id) == 1
    assert (tmp_path / "meetings" / meeting_id / "audio.pcm").stat().st_size == 60 * 320 * 2


def test_audio_sequence_is_idempotent_and_rejects_gaps(tmp_path):
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("meeting-1", Language.ZH, "测试员")
    assert store.append_audio("meeting-1", 0, 0, b"\x00\x00")
    assert not store.append_audio("meeting-1", 0, 0, b"\x00\x00")
    try:
        store.append_audio("meeting-1", 2, 1, b"\x00\x00")
    except ValueError as exc:
        assert "audio gap" in str(exc)
    else:
        raise AssertionError("missing audio frame was accepted")
    assert store.audio_position("meeting-1") == (0, 1)


def test_wake_answer_emits_stream_and_returns_to_listening(monkeypatch, tmp_path):
    import src.agent as agent
    import src.asr as asr
    import src.search as search
    import src.server as server
    import src.tts as tts

    class WakeASR:
        async def transcribe(self, segment):
            return "小会，刚才决定了什么？"

    async def fake_stream(messages):
        assert "小会，刚才决定了什么" in messages[-1]["content"]
        yield "已记录"

    async def fake_tts(text, language):
        assert text == "已记录"
        return b"ID3"

    monkeypatch.setattr(server, "storage", Storage(tmp_path / "meetings.db", tmp_path))
    monkeypatch.setattr(asr, "get_asr", lambda: WakeASR())
    monkeypatch.setattr(search, "search_local", lambda query: [{
        "source": "local", "title": "sample", "snippet": "证据", "score": 1.0
    }])
    monkeypatch.setattr(agent, "stream_answer", fake_stream)
    monkeypatch.setattr(tts, "synthesize_speech", fake_tts)
    meeting_id = f"wake-{uuid.uuid4().hex}"
    events = []
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/meetings/{meeting_id}") as ws:
            ws.send_json({"type": "config", "language": "zh", "speaker": "测试员"})
            for seq in range(60):
                pcm = struct.pack("<320h", *([10000] * 320 if seq < 30 else [0] * 320))
                ws.send_bytes(struct.pack("<IQ", seq, seq * 320) + pcm)
            for _ in range(70):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "asr.final":
                    break
            ws.send_json({"type": "end_meeting"})
            for _ in range(110):
                event = ws.receive_json()
                events.append(event)
                if event["type"] == "agent.state" and event["data"]["state"] == "ENDED":
                    break
    types = [event["type"] for event in events]
    assert "search.result" in types
    assert "llm.delta" in types
    assert "llm.done" in types
    assert "tts.audio" in types
    assert any(event["type"] == "agent.state" and event["data"]["state"] == "LISTENING"
               for event in events)
