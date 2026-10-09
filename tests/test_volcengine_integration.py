"""Cloud ASR frames reach the existing transcript and minutes pipeline."""
from __future__ import annotations

import asyncio
import struct

from fastapi.testclient import TestClient

from src.models import ConfigMessage, Language
from src.server import app
from src.storage import Storage
from src.volcengine_asr import TranscriptEvent


def test_meeting_requires_cloud_asr_key_before_creating_record(monkeypatch, tmp_path):
    import src.server as server

    store = Storage(tmp_path / "meetings.db", tmp_path)
    monkeypatch.setattr(server, "storage", store)
    monkeypatch.setattr(server.config, "volc_api_key", "")
    with TestClient(app) as client:
        assert client.get("/health").json()["asr_configured"] is False
        with client.websocket_connect("/ws/meetings/no-key") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            event = ws.receive_json()
            assert event["type"] == "error"
            assert "VOLC_API_KEY" in event["data"]["message"]
    assert store.get_meeting("no-key") is None


def test_cloud_auth_failure_prevents_recording_start(monkeypatch, tmp_path):
    import src.server as server
    import src.volcengine_asr as volcengine_asr

    class RejectedVolcengine:
        def __init__(self, api_key, resource_id):
            pass

        async def connect(self):
            raise volcengine_asr.VolcengineASRError("火山引擎拒绝 ASR 连接（HTTP 403）")

        async def close(self):
            pass

    store = Storage(tmp_path / "meetings.db", tmp_path)
    monkeypatch.setattr(server, "storage", store)
    monkeypatch.setattr(server.config, "volc_api_key", "test-key")
    monkeypatch.setattr(volcengine_asr, "VolcengineASRClient", RejectedVolcengine)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/meetings/rejected") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            event = ws.receive_json()
            assert event["type"] == "error"
            assert "HTTP 403" in event["data"]["message"]
            assert event["data"]["recoverable"] is False
    assert store.audio_position("rejected") == (-1, 0)


def test_volcengine_stream_reuses_meeting_pipeline(monkeypatch, tmp_path):
    import src.server as server
    import src.volcengine_asr as volcengine_asr

    clients = []

    class FakeVolcengine:
        def __init__(self, api_key, resource_id):
            assert api_key == "test-key"
            assert resource_id == "volc.seedasr.sauc.duration"
            self.audio = []
            self.responses = asyncio.Queue()
            self.closed = False
            clients.append(self)

        async def connect(self):
            pass

        async def send_pcm(self, pcm):
            if self is clients[0] and len(self.audio) == 2:
                raise ConnectionError("simulated upstream disconnect")
            self.audio.append(pcm)
            if len(self.audio) == 2:
                await self.responses.put(TranscriptEvent("今天决", False, speaker_id="7"))

        async def finish(self):
            await self.responses.put(TranscriptEvent(
                "今天决定采用方案A", True, speaker_id="7", language="yue", end_ms=400,
            ))
            await self.responses.put(None)

        async def events(self):
            while (event := await self.responses.get()) is not None:
                yield event

        async def close(self):
            self.closed = True

    monkeypatch.setattr(server.config, "volc_api_key", "test-key")
    monkeypatch.setattr(volcengine_asr, "VolcengineASRClient", FakeVolcengine)
    store = Storage(tmp_path / "meetings.db", tmp_path)
    monkeypatch.setattr(server, "storage", store)
    events = []

    with TestClient(app) as client:
        with client.websocket_connect("/ws/meetings/volc-test") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            for seq in range(20):
                frame = struct.pack("<IQ", seq, seq * 320) + b"\x00\x01" * 320
                ws.send_bytes(frame)
                if seq == 0:
                    ws.send_bytes(frame)  # persisted retransmission must not reach provider twice
            ws.send_json({"type": "flush"})
            while not any(e["type"] == "asr.final" for e in events):
                events.append(ws.receive_json())
            ws.send_json({"type": "end_meeting"})
            while not any(e["type"] == "agent.state" and e["data"]["state"] == "ENDED"
                          for e in events):
                events.append(ws.receive_json())

    assert len(clients) == 2
    assert all(upstream.closed for upstream in clients)
    assert sum(map(len, clients[1].audio)) == 20 * 320 * 2  # all frames replayed
    assert any(e["type"] == "asr.partial" and e["data"]["text"] == "今天决" for e in events)
    assert any(e["type"] == "asr.final" and e["data"]["language"] == "yue"
               and e["data"]["speaker"].startswith("说话人") for e in events)
    assert next(e for e in events if e["type"] == "asr.final")["data"]["seq"] == 19
    assert any(e["type"] == "minutes.ready" and e["data"]["phase"] == "final" for e in events)
    assert store.count_utterances("volc-test") == 1
    assert store.cloud_asr_checkpoint("volc-test") == 6400


def test_cloud_asr_replays_persisted_audio_after_browser_reconnect(monkeypatch, tmp_path):
    import src.server as server
    import src.volcengine_asr as volcengine_asr

    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("replay-test", Language.AUTO, "未知发言人")
    for seq in range(20):
        store.append_audio("replay-test", seq, seq * 320, b"\x00\x01" * 320)
    monkeypatch.setattr(server, "storage", store)
    monkeypatch.setattr(server.config, "volc_api_key", "test-key")
    sent = []

    class FakeVolcengine:
        def __init__(self, api_key, resource_id):
            self.responses = asyncio.Queue()

        async def connect(self):
            pass

        async def send_pcm(self, pcm):
            sent.append(pcm)

        async def finish(self):
            await self.responses.put(TranscriptEvent(
                "断线前的话", True, speaker_id="1", language="zh", end_ms=200,
            ))
            await self.responses.put(None)

        async def events(self):
            while (event := await self.responses.get()) is not None:
                yield event

        async def close(self):
            pass

    monkeypatch.setattr(volcengine_asr, "VolcengineASRClient", FakeVolcengine)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/meetings/replay-test") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            ws.send_json({"type": "flush"})
            while (event := ws.receive_json())["type"] != "asr.final":
                pass
            assert event["data"]["text"] == "断线前的话"
            assert event["data"]["seq"] == 9
            ws.send_json({"type": "end_meeting"})
            while not ((event := ws.receive_json())["type"] == "agent.state"
                       and event["data"]["state"] == "ENDED"):
                pass

    assert sum(map(len, sent)) == 20 * 320 * 2
    assert store.cloud_asr_checkpoint("replay-test") == 6400
    assert store.count_utterances("replay-test") == 1


def test_failed_asr_finish_keeps_meeting_retryable(monkeypatch, tmp_path):
    import src.server as server
    import src.volcengine_asr as volcengine_asr

    store = Storage(tmp_path / "meetings.db", tmp_path)
    monkeypatch.setattr(server, "storage", store)
    monkeypatch.setattr(server.config, "volc_api_key", "test-key")
    connections = []

    class FlakyVolcengine:
        def __init__(self, api_key, resource_id):
            self.responses = asyncio.Queue()
            connections.append(self)

        async def connect(self):
            pass

        async def send_pcm(self, pcm):
            pass

        async def finish(self):
            if self is connections[0]:
                raise ConnectionError("temporary upstream failure")
            await self.responses.put(TranscriptEvent(
                "今天决定采用方案A", True, language="zh", end_ms=400,
            ))
            await self.responses.put(None)

        async def events(self):
            while (event := await self.responses.get()) is not None:
                yield event

        async def close(self):
            pass

    monkeypatch.setattr(volcengine_asr, "VolcengineASRClient", FlakyVolcengine)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/meetings/retry-finish") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            for seq in range(20):
                ws.send_bytes(struct.pack("<IQ", seq, seq * 320) + b"\x00\x01" * 320)
            ws.send_json({"type": "end_meeting"})
            first = []
            while not (first and first[-1]["type"] == "agent.state" and
                       first[-1]["data"]["state"] == "LISTENING" and
                       any(e["type"] == "error" for e in first)):
                first.append(ws.receive_json())
            assert any(e["type"] == "error" and e["data"]["code"] == "ASR_FAILED"
                       for e in first)
            assert not any(e["type"] == "minutes.ready" for e in first)
            ws.send_json({"type": "end_meeting"})
            second = []
            while not (second and second[-1]["type"] == "agent.state" and
                       second[-1]["data"]["state"] == "ENDED"):
                second.append(ws.receive_json())

    assert len(connections) == 2
    assert store.count_utterances("retry-finish") == 1
    assert store.get_minutes("retry-finish") is not None


def test_corrected_final_at_same_audio_time_is_not_saved_twice(monkeypatch, tmp_path):
    import src.server as server

    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("corrected-final", Language.AUTO, "未知发言人")
    for seq in range(20):
        store.append_audio("corrected-final", seq, seq * 320, b"\x00\x01" * 320)
    monkeypatch.setattr(server, "storage", store)
    monkeypatch.setattr(server.config, "volc_api_key", "test-key")

    class FakeSocket:
        def __init__(self):
            self.events = []

        async def send_json(self, event):
            self.events.append(event)

    socket = FakeSocket()

    async def scenario():
        session = server.MeetingSession(socket, "corrected-final", ConfigMessage())
        await session._handle_volc_event(TranscriptEvent(
            "客服资料需要同步更新。", True, language="zh", end_ms=200,
        ))
        await session._handle_volc_event(TranscriptEvent(
            "客服资料需要同步更新", True, language="zh", end_ms=200,
        ))

    asyncio.run(scenario())
    assert store.count_utterances("corrected-final") == 1
    assert sum(event["type"] == "asr.final" for event in socket.events) == 1
    store.close()
