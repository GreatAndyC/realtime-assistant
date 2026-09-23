"""Offline protocol checks; no provider connection or paid request occurs."""

import asyncio
import gzip
import json
import struct
import wave

import pytest

from src import volcengine_asr as asr


def provider_frame(result, *, last=False, compressed=True):
    payload = json.dumps(result, ensure_ascii=False).encode()
    if compressed:
        payload = gzip.compress(payload)
    header = bytes((0x11, 0x93 if last else 0x91, 0x11 if compressed else 0x10, 0))
    return header + struct.pack(">iI", -1 if last else 1, len(payload)) + payload


def decode_client_frame(frame):
    assert frame[0] == 0x11
    sequence, size = struct.unpack_from(">iI", frame, 4)
    assert len(frame) == size + 12
    return frame[1], frame[2], sequence, gzip.decompress(frame[12:])


class FakeWebSocket:
    def __init__(self, *, auto_final=None):
        self.sent = []
        self.responses = asyncio.Queue()
        self.auto_final = auto_final
        self.close_count = 0

    async def send(self, frame):
        self.sent.append(frame)
        if self.auto_final is not None and frame[1] == 0x23:
            await self.responses.put(provider_frame(self.auto_final, last=True))

    async def recv(self):
        return await self.responses.get()

    async def close(self):
        self.close_count += 1


def test_connect_packet_and_pcm_final_packet(monkeypatch):
    ws = FakeWebSocket()
    captured = {}

    async def open_fake(url, headers):
        captured.update(url=url, headers=headers)
        return ws

    monkeypatch.setattr(asr, "_open_websocket", open_fake)

    async def scenario():
        client = asr.VolcengineASRClient("fake-key")
        await client.connect()
        await client.send_pcm(b"\x01\x00" * 3200)
        await client.send_pcm(b"\x02\x00" * 3200)
        await client.finish()
        await client.close()
        await client.close()

    asyncio.run(scenario())
    assert captured["url"].endswith("/api/v3/sauc/bigmodel_async")
    assert captured["headers"]["X-Api-Key"] == "fake-key"
    assert captured["headers"]["X-Api-Resource-Id"] == "volc.seedasr.sauc.duration"
    assert captured["headers"]["X-Api-Sequence"] == "-1"
    assert captured["headers"]["X-Api-Connect-Id"] == captured["headers"]["X-Api-Request-Id"]
    assert len(ws.sent) == 4
    header, serial, sequence, payload = decode_client_frame(ws.sent[0])
    assert (header, serial, sequence) == (0x11, 0x11, 1)
    request = json.loads(payload)
    assert request["audio"] == {
        "format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1,
    }
    assert request["request"]["enable_speaker_info"] is True
    assert request["request"]["enable_nonstream"] is True
    assert request["request"]["show_utterances"] is True
    assert request["request"]["ssd_version"] == "200"
    assert request["request"]["enable_lid"] is True
    audio_frames = [decode_client_frame(frame) for frame in ws.sent[1:]]
    assert [(head, serial, seq) for head, serial, seq, _ in audio_frames] == [
        (0x21, 0x01, 2), (0x21, 0x01, 3), (0x23, 0x01, -4),
    ]
    assert b"".join(payload for _, _, _, payload in audio_frames) == (
        b"\x01\x00" * 3200 + b"\x02\x00" * 3200
    )
    assert len(audio_frames[0][3]) == 6398  # sent at the first 200 ms mark
    assert ws.close_count == 1


def test_utterances_emit_new_finals_once_and_language(monkeypatch):
    ws = FakeWebSocket()

    async def open_fake(url, headers):
        return ws

    monkeypatch.setattr(asr, "_open_websocket", open_fake)

    async def scenario():
        client = asr.VolcengineASRClient("fake-key")
        await client.connect()
        interim = {"result": {"utterances": [
            {"text": "大家", "start_time": 0, "end_time": 500, "definite": False}
        ]}}
        final = {"result": {"utterances": [
            {"text": "大家好", "start_time": 0, "end_time": 700,
             "definite": True, "speaker_id": 2,
             "additions": {"lid_lang": "speech_dia_cant"}},
            {"text": "下一句", "start_time": 800, "end_time": 1300, "definite": False},
        ]}}
        await ws.responses.put(provider_frame({"result": {}}))
        await ws.responses.put(provider_frame(interim))
        await ws.responses.put(provider_frame(interim))
        await ws.responses.put(provider_frame(final))
        await ws.responses.put(provider_frame(final, last=True))
        events = [event async for event in client.events()]
        await client.close()
        return client, events

    client, events = asyncio.run(scenario())
    assert client.finished
    assert [(e.text, e.final) for e in events] == [
        ("大家", False), ("大家好", True), ("下一句", False), ("下一句", True),
    ]
    assert events[1].speaker_id == "2"
    assert events[1].language == "yue"
    assert events[1].end_ms == 700


def test_error_response_and_odd_pcm(monkeypatch):
    ws = FakeWebSocket()

    async def open_fake(url, headers):
        return ws

    monkeypatch.setattr(asr, "_open_websocket", open_fake)

    async def scenario():
        client = asr.VolcengineASRClient("fake-key")
        await client.connect()
        with pytest.raises(ValueError, match="PCM16"):
            await client.send_pcm(b"\x00")
        message = b'{"message":"invalid request"}'
        await ws.responses.put(bytes((0x11, 0xF0, 0x10, 0)) +
                               struct.pack(">II", 45000001, len(message)) + message)
        with pytest.raises(asr.VolcengineASRError, match="45000001"):
            _ = [event async for event in client.events()]
        await client.close()

    asyncio.run(scenario())


def test_wav_cli_streams_pcm_without_network(tmp_path, monkeypatch, capsys):
    pcm = b"\x01\x00" * 3200
    wav_path = tmp_path / "sample.wav"
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(pcm)

    ws = FakeWebSocket(auto_final={"result": {"utterances": [
        {"text": "测试完成", "start_time": 0, "end_time": 200,
         "definite": True, "speaker_id": 1,
         "additions": {"lid_lang": "speech_mand"}},
    ]}})

    async def open_fake(url, headers):
        return ws

    monkeypatch.setattr(asr, "_open_websocket", open_fake)
    monkeypatch.setenv("VOLC_API_KEY", "fake-key")
    asyncio.run(asr._run_wav(str(wav_path)))
    output = json.loads(capsys.readouterr().out)
    assert output["text"] == "测试完成"
    assert output["language"] == "zh"
    assert output["final"] is True
    audio_frames = [decode_client_frame(frame) for frame in ws.sent[1:]]
    assert audio_frames[-1][:3] == (0x23, 0x01, -3)
    assert b"".join(frame[3] for frame in audio_frames) == pcm
