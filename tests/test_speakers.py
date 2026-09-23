"""Provider speaker IDs receive stable anonymous labels within a connection."""
from __future__ import annotations

import asyncio
import struct

from fastapi.testclient import TestClient

from src.server import app
from src.storage import Storage
from src.volcengine_asr import TranscriptEvent


def _setup(monkeypatch, tmp_path, per_connection, *, drop_after_first=None):
    import src.server as server
    import src.volcengine_asr as volcengine_asr

    store = Storage(tmp_path / "meetings.db", tmp_path)
    monkeypatch.setattr(server, "storage", store)
    monkeypatch.setattr(server.config, "volc_api_key", "test-key")
    clients = []

    class FakeVolcengine:
        def __init__(self, api_key, resource_id):
            self.responses = asyncio.Queue()
            self.frames = 0
            self.number = len(clients)
            clients.append(self)

        async def connect(self):
            pass

        async def send_pcm(self, pcm):
            self.frames += 1
            if self.number == 0 and self.frames == drop_after_first:
                raise ConnectionError("simulated upstream disconnect")
            for count, text, speaker_id in per_connection[self.number]:
                if self.frames == count:
                    await self.responses.put(TranscriptEvent(
                        text, True, speaker_id=speaker_id,
                        language="zh", end_ms=count * 20,
                    ))

        async def finish(self):
            await self.responses.put(None)

        async def events(self):
            while (event := await self.responses.get()) is not None:
                yield event

        async def close(self):
            pass

    monkeypatch.setattr(volcengine_asr, "VolcengineASRClient", FakeVolcengine)
    return store, clients


def _send(ws, start, end):
    for seq in range(start, end):
        ws.send_bytes(struct.pack("<IQ", seq, seq * 320) + b"\x00\x00" * 320)


def _finals(ws, count):
    finals = []
    while len(finals) < count:
        event = ws.receive_json()
        if event["type"] == "asr.final":
            finals.append(event["data"])
    return finals


def _end(ws):
    ws.send_json({"type": "end_meeting"})
    while not ((event := ws.receive_json())["type"] == "agent.state"
               and event["data"]["state"] == "ENDED"):
        pass


def test_alternating_provider_speakers_keep_their_labels(monkeypatch, tmp_path):
    store, clients = _setup(monkeypatch, tmp_path, [[
        (20, "第一句", "A"), (40, "第二句", "B"),
        (60, "第三句", "A"), (80, "第四句", "B"),
    ]])

    with TestClient(app) as client:
        with client.websocket_connect("/ws/meetings/speaker-test") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            _send(ws, 0, 80)
            ws.send_json({"type": "flush"})
            finals = _finals(ws, 4)
            _end(ws)

    assert len(clients) == 1
    assert [item["speaker"] for item in finals] == [
        "说话人1", "说话人2", "说话人1", "说话人2",
    ]
    assert [u.speaker for u in store.list_utterances("speaker-test")] == [
        "说话人1", "说话人2", "说话人1", "说话人2",
    ]


def test_upstream_reconnect_uses_new_speaker_number(monkeypatch, tmp_path):
    store, clients = _setup(monkeypatch, tmp_path, [
        [(20, "重连前", "A")], [(20, "重连后", "A")],
    ], drop_after_first=21)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/meetings/speaker-reconnect") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            _send(ws, 0, 20)
            first = _finals(ws, 1)[0]
            _send(ws, 20, 40)
            second = _finals(ws, 1)[0]
            _end(ws)

    assert len(clients) == 2
    assert (first["speaker"], second["speaker"]) == ("说话人1", "说话人2")
    assert [u.speaker for u in store.list_utterances("speaker-reconnect")] == [
        "说话人1", "说话人2",
    ]
