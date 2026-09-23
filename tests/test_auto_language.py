"""Cloud ASR language detection is applied to each finalized utterance."""
from __future__ import annotations

import asyncio
import struct

from fastapi.testclient import TestClient

from src.server import app
from src.storage import Storage
from src.volcengine_asr import TranscriptEvent


def test_auto_language_is_selected_for_each_cloud_utterance(monkeypatch, tmp_path):
    import src.server as server
    import src.volcengine_asr as volcengine_asr

    store = Storage(tmp_path / "meetings.db", tmp_path)
    monkeypatch.setattr(server, "storage", store)
    monkeypatch.setattr(server.config, "volc_api_key", "test-key")

    class FakeVolcengine:
        def __init__(self, api_key, resource_id):
            self.responses = asyncio.Queue()
            self.frames = 0

        async def connect(self):
            pass

        async def send_pcm(self, pcm):
            self.frames += 1
            if self.frames in (20, 40):
                await self.responses.put(TranscriptEvent(
                    "普通话" if self.frames == 20 else "廣東話", True,
                    speaker_id="A", language="zh" if self.frames == 20 else "yue",
                    end_ms=self.frames * 20,
                ))

        async def finish(self):
            await self.responses.put(None)

        async def events(self):
            while (event := await self.responses.get()) is not None:
                yield event

        async def close(self):
            pass

    monkeypatch.setattr(volcengine_asr, "VolcengineASRClient", FakeVolcengine)
    with TestClient(app) as client:
        with client.websocket_connect("/ws/meetings/language-test") as ws:
            ws.send_json({"type": "config", "language": "auto"})
            for seq in range(40):
                ws.send_bytes(struct.pack("<IQ", seq, seq * 320) + b"\x00\x00" * 320)
            ws.send_json({"type": "flush"})
            finals = []
            while len(finals) < 2:
                event = ws.receive_json()
                if event["type"] == "asr.final":
                    finals.append(event["data"])
            ws.send_json({"type": "end_meeting"})
            while (event := ws.receive_json())["type"] != "agent.state" or event["data"]["state"] != "ENDED":
                pass

    assert [(item["text"], item["language"], item["seq"]) for item in finals] == [
        ("普通话", "zh", 19), ("廣東話", "yue", 39),
    ]
    assert [(u.text, u.language.value) for u in store.list_utterances("language-test")] == [
        ("普通话", "zh"), ("廣東話", "yue"),
    ]
