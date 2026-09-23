"""End-to-end WebSocket framing and meeting persistence without external models."""
from __future__ import annotations

import asyncio
import struct
import uuid

from fastapi.testclient import TestClient

from src.models import Language
from src.agent import extract_wake_query, is_stop_command
from src.server import app
from src.storage import Storage


class FakeASR:
    async def transcribe(self, segment):
        return "今天决定采用方案A"

    async def transcribe_with_language(self, segment):
        return await self.transcribe(segment), Language.ZH


def test_wake_phrase_handles_whisper_homophone_only_at_sentence_start():
    assert extract_wake_query("小慧，刚才决定了什么？") == "刚才决定了什么？"
    assert extract_wake_query("我们刚才提到小慧") is None
    assert is_stop_command("小慧暂停")
    assert is_stop_command("小會，唔好講啦")
    assert not is_stop_command("我们要暂停讨论")


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
    import src.tool_agent as tool_agent
    import src.server as server
    import src.tts as tts

    class WakeASR:
        async def transcribe(self, segment):
            return "小会，刚才决定了什么？"

        async def transcribe_with_language(self, segment):
            return await self.transcribe(segment), Language.YUE

    async def fake_stream(messages):
        assert "小会，刚才决定了什么" in messages[-1]["content"]
        assert "粤语口语" in messages[0]["content"]
        yield "已记录"

    async def fake_tts(text, language):
        assert text == "已记录"
        assert language == Language.YUE
        return b"ID3"

    monkeypatch.setattr(server, "storage", Storage(tmp_path / "meetings.db", tmp_path))
    monkeypatch.setattr(asr, "get_asr", lambda: WakeASR())
    async def fake_evidence(question, store, meeting_id, emit):
        await emit({"step": 1, "tool": "search_local_documents", "label": "本地资料", "status": "done", "count": 1})
        return [{"source": "local", "title": "sample", "snippet": "证据", "score": 1.0}]

    monkeypatch.setattr(tool_agent, "gather_agent_evidence", fake_evidence)
    monkeypatch.setattr(agent, "stream_answer", fake_stream)
    monkeypatch.setattr(tts, "synthesize_speech", fake_tts)
    meeting_id = f"wake-{uuid.uuid4().hex}"
    events = []
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/meetings/{meeting_id}") as ws:
            ws.send_json({"type": "config", "language": "auto", "speaker": "测试员"})
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
    assert any(e["type"] == "asr.final" and e["data"]["language"] == "yue" for e in events)
    assert any(event["type"] == "agent.state" and event["data"]["state"] == "LISTENING"
               for event in events)


def test_stop_answer_cancels_generation_and_meeting_keeps_listening(monkeypatch, tmp_path):
    import src.agent as agent
    import src.asr as asr
    import src.tool_agent as tool_agent
    import src.server as server

    class TwoUtterances:
        count = 0

        async def transcribe_with_language(self, segment):
            self.count += 1
            return ("小慧，讲一下方案" if self.count == 1 else "继续开会"), Language.ZH

        async def transcribe(self, segment):
            return ""

    async def slow_answer(messages):
        yield "正在回答"
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "storage", Storage(tmp_path / "meetings.db", tmp_path))
    monkeypatch.setattr(asr, "get_asr", lambda: TwoUtterances())
    async def fake_evidence(question, store, meeting_id, emit):
        return [{"source": "local", "title": "sample", "snippet": "证据", "score": 1.0}]

    monkeypatch.setattr(tool_agent, "gather_agent_evidence", fake_evidence)
    monkeypatch.setattr(agent, "stream_answer", slow_answer)
    meeting_id = f"stop-{uuid.uuid4().hex}"

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/meetings/{meeting_id}") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            for seq in range(60):
                pcm = struct.pack("<320h", *([10000] * 320 if seq < 30 else [0] * 320))
                ws.send_bytes(struct.pack("<IQ", seq, seq * 320) + pcm)
            while ws.receive_json()["type"] != "llm.delta":
                pass
            ws.send_json({"type": "stop_answer"})
            events = []
            while not any(e["type"] == "tts.stop" for e in events):
                events.append(ws.receive_json())
            assert any(e["type"] == "agent.state" and e["data"]["state"] == "LISTENING"
                       for e in events)
            for seq in range(60, 120):
                pcm = struct.pack("<320h", *([10000] * 320 if seq < 90 else [0] * 320))
                ws.send_bytes(struct.pack("<IQ", seq, seq * 320) + pcm)
            events = []
            while not any(e["type"] == "asr.final" and e["data"]["text"] == "继续开会"
                          for e in events):
                events.append(ws.receive_json())
            ws.send_json({"type": "end_meeting"})
            while not (event := ws.receive_json())["type"] == "agent.state" or event["data"]["state"] != "ENDED":
                pass
