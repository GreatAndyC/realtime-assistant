"""Browser audio framing, cloud transcript events and meeting persistence."""
from __future__ import annotations

import asyncio
import struct
import uuid

from fastapi.testclient import TestClient

from src.agent import extract_wake_query, is_stop_command
from src.models import Language
from src.server import app
from src.storage import Storage
from src.volcengine_asr import TranscriptEvent


def _fake_cloud(monkeypatch, tmp_path, utterances):
    """Emit provider results after the specified count of browser PCM frames."""
    import src.server as server
    import src.volcengine_asr as volcengine_asr

    store = Storage(tmp_path / "meetings.db", tmp_path)
    monkeypatch.setattr(server, "storage", store)
    monkeypatch.setattr(server.config, "volc_api_key", "test-key")

    class FakeVolcengine:
        def __init__(self, api_key, resource_id):
            assert api_key == "test-key"
            self.responses = asyncio.Queue()
            self.frames = 0

        async def connect(self):
            pass

        async def send_pcm(self, pcm):
            self.frames += 1
            for count, text, language in utterances:
                if self.frames == count:
                    await self.responses.put(TranscriptEvent(
                        text, True, speaker_id="provider-1", language=language,
                        end_ms=count * 20,
                    ))

        async def finish(self):
            await self.responses.put(None)

        async def events(self):
            while (event := await self.responses.get()) is not None:
                yield event

        async def close(self):
            pass

    monkeypatch.setattr(volcengine_asr, "VolcengineASRClient", FakeVolcengine)
    return store


def _send_audio(ws, start, end):
    for seq in range(start, end):
        ws.send_bytes(struct.pack("<IQ", seq, seq * 320) + b"\x10\x00" * 320)


def _receive_until(ws, predicate, limit=150):
    events = []
    for _ in range(limit):
        event = ws.receive_json()
        events.append(event)
        if predicate(event):
            return events
    raise AssertionError("Expected WebSocket event was not received")


def test_wake_phrase_handles_recognition_homophone_only_at_sentence_start():
    assert extract_wake_query("小慧，刚才决定了什么？") == "刚才决定了什么？"
    assert extract_wake_query("我们刚才提到小慧") is None
    assert is_stop_command("小慧暂停")
    assert is_stop_command("小會，唔好講啦")
    assert not is_stop_command("我们要暂停讨论")


def test_websocket_audio_to_final_minutes(monkeypatch, tmp_path):
    store = _fake_cloud(monkeypatch, tmp_path, [(60, "今天决定采用方案A", "zh")])
    meeting_id = f"test-{uuid.uuid4().hex}"

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/meetings/{meeting_id}") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            _send_audio(ws, 0, 60)
            ws.send_json({"type": "flush"})
            events = _receive_until(ws, lambda e: e["type"] == "asr.final")
            ws.send_json({"type": "end_meeting"})
            events += _receive_until(
                ws, lambda e: e["type"] == "agent.state" and e["data"]["state"] == "ENDED",
            )

    assert any(e["type"] == "ack" and e["data"]["seq"] == 59 for e in events)
    assert any(e["type"] == "asr.final" and e["data"]["text"] == "今天决定采用方案A"
               for e in events)
    assert any(e["type"] == "minutes.ready" and e["data"]["phase"] == "final"
               for e in events)
    assert store.count_utterances(meeting_id) == 1
    assert (tmp_path / "meetings" / meeting_id / "audio.pcm").stat().st_size == 60 * 320 * 2


def test_audio_sequence_is_idempotent_and_rejects_gaps(tmp_path):
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("meeting-1", Language.ZH, "测试员")
    assert store.append_audio("meeting-1", 0, 0, b"\x00\x00")
    assert not store.append_audio("meeting-1", 0, 0, b"\x00\x00")
    try:
        store.append_audio("meeting-1", 0, 0, b"\x01\x00")
    except ValueError as exc:
        assert "conflicts" in str(exc)
    else:
        raise AssertionError("conflicting retransmission was accepted")
    try:
        store.append_audio("meeting-1", 2, 1, b"\x00\x00")
    except ValueError as exc:
        assert "audio gap" in str(exc)
    else:
        raise AssertionError("missing audio frame was accepted")
    assert store.audio_position("meeting-1") == (0, 1)


def test_wake_answer_emits_stream_and_returns_to_listening(monkeypatch, tmp_path):
    import src.agent as agent
    import src.tool_agent as tool_agent
    import src.tts as tts

    store = _fake_cloud(monkeypatch, tmp_path, [(60, "小会，刚才决定了什么？", "yue")])

    async def fake_stream(messages):
        assert "刚才决定了什么" in messages[-1]["content"]
        assert "粤语口语" in messages[0]["content"]
        yield "已记录"

    async def fake_tts(text, language):
        assert text == "已记录"
        assert language == Language.YUE
        return b"ID3"

    async def fake_evidence(question, store, meeting_id, emit):
        await emit({"step": 1, "tool": "search_local_documents", "label": "本地资料",
                    "status": "done", "count": 1})
        return [{"source": "local", "title": "sample", "snippet": "证据", "score": 1.0}]

    monkeypatch.setattr(tool_agent, "gather_agent_evidence", fake_evidence)
    monkeypatch.setattr(agent, "stream_answer", fake_stream)
    monkeypatch.setattr(tts, "synthesize_speech", fake_tts)
    meeting_id = f"wake-{uuid.uuid4().hex}"
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/meetings/{meeting_id}") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            _send_audio(ws, 0, 60)
            events = _receive_until(ws, lambda e: e["type"] == "tts.audio")
            ws.send_json({"type": "end_meeting"})
            events += _receive_until(
                ws, lambda e: e["type"] == "agent.state" and e["data"]["state"] == "ENDED",
            )

    types = [event["type"] for event in events]
    assert "search.result" in types
    assert "llm.delta" in types
    assert "llm.done" in types
    assert "tts.audio" in types
    assert any(e["type"] == "asr.final" and e["data"]["language"] == "yue" for e in events)
    assert any(e["type"] == "agent.state" and e["data"]["state"] == "LISTENING"
               for e in events)
    assert store.count_utterances(meeting_id) == 1


def test_stop_answer_cancels_generation_and_meeting_keeps_listening(monkeypatch, tmp_path):
    import src.agent as agent
    import src.tool_agent as tool_agent

    store = _fake_cloud(monkeypatch, tmp_path, [
        (60, "小慧，讲一下方案", "zh"), (120, "继续开会", "zh"),
    ])

    async def slow_answer(messages):
        yield "正在回答"
        await asyncio.Event().wait()

    async def fake_evidence(question, store, meeting_id, emit):
        return [{"source": "local", "title": "sample", "snippet": "证据", "score": 1.0}]

    monkeypatch.setattr(tool_agent, "gather_agent_evidence", fake_evidence)
    monkeypatch.setattr(agent, "stream_answer", slow_answer)
    meeting_id = f"stop-{uuid.uuid4().hex}"
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/meetings/{meeting_id}") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            _send_audio(ws, 0, 60)
            _receive_until(ws, lambda e: e["type"] == "llm.delta")
            ws.send_json({"type": "stop_answer"})
            events = _receive_until(ws, lambda e: e["type"] == "tts.stop")
            assert any(e["type"] == "agent.state" and e["data"]["state"] == "LISTENING"
                       for e in events)
            _send_audio(ws, 60, 120)
            _receive_until(
                ws, lambda e: e["type"] == "asr.final" and e["data"]["text"] == "继续开会",
            )
            ws.send_json({"type": "end_meeting"})
            _receive_until(
                ws, lambda e: e["type"] == "agent.state" and e["data"]["state"] == "ENDED",
            )

    assert store.count_utterances(meeting_id) == 2
