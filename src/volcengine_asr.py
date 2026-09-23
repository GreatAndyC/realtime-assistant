"""Volcengine Doubao ASR 2.0 streaming WebSocket adapter.

Wire format and options follow the official streaming ASR API:
https://www.volcengine.com/docs/6561/1354869?lang=zh
No credentials or audio are sent until ``connect`` is called explicitly.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import gzip
import json
import os
import struct
import uuid
import wave
from typing import AsyncIterator, Any


DEFAULT_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"
CHUNK_BYTES = 6400  # 200 ms of 16 kHz mono PCM16


class VolcengineASRError(RuntimeError):
    """Provider, transport, or wire-protocol failure."""


@dataclass(frozen=True)
class TranscriptEvent:
    text: str
    final: bool
    speaker_id: str | None = None
    language: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None


async def _open_websocket(url: str, headers: dict[str, str]) -> Any:
    # The legacy client entry point keeps `extra_headers` stable across
    # websockets 12 through 15, which this project's requirements allow.
    from websockets.legacy.client import connect

    return await connect(url, extra_headers=headers, open_timeout=10, ping_interval=20)


def _pack(message_type: int, flags: int, serialization: int, payload: bytes, sequence: int) -> bytes:
    compressed = gzip.compress(payload)
    header = bytes((0x11, (message_type << 4) | flags, (serialization << 4) | 1, 0))
    return header + struct.pack(">iI", sequence, len(compressed)) + compressed


def _parse(frame: bytes) -> tuple[dict[str, Any], bool]:
    if len(frame) < 8 or frame[0] >> 4 != 1:
        raise VolcengineASRError("Invalid ASR response header")
    header_length = (frame[0] & 0x0F) * 4
    if header_length < 4 or len(frame) < header_length + 4:
        raise VolcengineASRError("Truncated ASR response header")
    message_type = frame[1] >> 4
    flags = frame[1] & 0x0F
    compression = frame[2] & 0x0F
    if message_type not in (9, 15):
        raise VolcengineASRError(f"Unexpected ASR response type {message_type}")
    offset = header_length
    sequence = None
    if message_type == 9 and flags in (1, 3):
        if len(frame) < offset + 8:
            raise VolcengineASRError("Truncated ASR sequence")
        sequence = struct.unpack_from(">i", frame, offset)[0]
        offset += 4
    elif message_type == 15:
        if len(frame) < offset + 8:
            raise VolcengineASRError("Truncated ASR error code")
        error_code = struct.unpack_from(">I", frame, offset)[0]
        offset += 4
    else:
        error_code = None
    size = struct.unpack_from(">I", frame, offset)[0]
    offset += 4
    if len(frame) != offset + size:
        raise VolcengineASRError("ASR payload length mismatch")
    payload = frame[offset:]
    if compression == 1:
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError) as exc:
            raise VolcengineASRError("Invalid compressed ASR response") from exc
    elif compression != 0:
        raise VolcengineASRError(f"Unsupported ASR compression {compression}")
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VolcengineASRError("Invalid ASR JSON response") from exc
    if message_type == 15:
        message = data.get("message", data) if isinstance(data, dict) else data
        raise VolcengineASRError(f"Volcengine ASR error {error_code}: {message}")
    if not isinstance(data, dict):
        raise VolcengineASRError("Unexpected ASR result shape")
    return data, flags in (2, 3) or (sequence is not None and sequence < 0)


def _language(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    value = value.lower()
    if value in ("speech_dia_cant", "singing_dia_cant", "yue-cn", "yue"):
        return "yue"
    if value in ("speech_mand", "singing_mand", "zh-cn", "zh"):
        return "zh"
    if value in ("speech_en", "singing_en", "en-us", "en"):
        return "en"
    return None


def _get_language(*objects: Any) -> str | None:
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for candidate in (obj, obj.get("additions")):
            if isinstance(candidate, dict):
                for key in ("lid_lang", "language", "lang_code"):
                    result = _language(candidate.get(key))
                    if result:
                        return result
    return None


class VolcengineASRClient:
    """One provider connection for one continuous microphone stream.

    ``send_pcm`` accepts arbitrary even-sized PCM16 fragments and sends ~200 ms
    provider packets. ``events`` must be consumed concurrently with sending.
    Reconnect/replay policy is intentionally owned by the caller.
    """

    def __init__(
        self,
        api_key: str,
        resource_id: str = DEFAULT_RESOURCE_ID,
        url: str = DEFAULT_URL,
    ) -> None:
        if not api_key:
            raise ValueError("VOLC_API_KEY is required")
        self._api_key = api_key
        self.resource_id = resource_id
        self.url = url
        self._ws: Any | None = None
        self._buffer = bytearray()
        self._send_lock = asyncio.Lock()
        self._sent_audio = False
        self._sequence = 0
        self._finish_sent = False
        self.finished = False
        self._closed = False
        self._final_utterances: set[tuple[Any, Any, str]] = set()
        self._last_partial: tuple[Any, Any, str] | None = None

    async def connect(self) -> None:
        if self._ws is not None or self._closed:
            raise VolcengineASRError("ASR client cannot be connected twice")
        request_id = str(uuid.uuid4())
        headers = {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Connect-Id": request_id,
            "X-Api-Sequence": "-1",
        }
        try:
            self._ws = await _open_websocket(self.url, headers)
            request = {
                "user": {"uid": str(uuid.uuid4())},
                "audio": {
                    "format": "pcm", "codec": "raw", "rate": 16000,
                    "bits": 16, "channel": 1,
                },
                "request": {
                    "model_name": "bigmodel",
                    "enable_punc": True,
                    "enable_ddc": True,
                    "enable_speaker_info": True,
                    "ssd_version": "200",
                    "enable_nonstream": True,
                    "show_utterances": True,
                    "enable_lid": True,
                },
            }
            self._sequence = 1
            await self._ws.send(_pack(1, 1, 1, json.dumps(request).encode("utf-8"), self._sequence))
        except Exception as exc:
            await self.close()
            raise VolcengineASRError("Could not connect to Volcengine ASR") from exc

    async def send_pcm(self, pcm: bytes) -> None:
        if len(pcm) % 2:
            raise ValueError("PCM16 must contain whole 16-bit samples")
        if not pcm:
            return
        async with self._send_lock:
            self._require_sending()
            self._buffer.extend(pcm)
            # Keep one PCM sample for a nonempty final packet; emit the first
            # ~200 ms packet immediately when its samples are available.
            while len(self._buffer) >= CHUNK_BYTES:
                chunk_size = min(CHUNK_BYTES, len(self._buffer) - 2)
                chunk = bytes(self._buffer[:chunk_size])
                try:
                    await self._ws.send(_pack(2, 1, 0, chunk, self._sequence + 1))
                except Exception as exc:
                    raise VolcengineASRError("Could not send audio to Volcengine ASR") from exc
                self._sequence += 1
                del self._buffer[:chunk_size]
                self._sent_audio = True

    async def finish(self) -> None:
        """Signal the last audio packet; `events` then drains the final result."""
        async with self._send_lock:
            if self._finish_sent:
                return
            self._require_sending()
            if not self._buffer and not self._sent_audio:
                raise ValueError("Cannot finish an empty ASR stream")
            chunk = bytes(self._buffer)
            try:
                await self._ws.send(_pack(2, 3, 0, chunk, -(self._sequence + 1)))
            except Exception as exc:
                raise VolcengineASRError("Could not finish Volcengine ASR stream") from exc
            self._buffer.clear()
            self._sequence += 1
            self._finish_sent = True

    def _require_sending(self) -> None:
        if self._ws is None or self._closed or self._finish_sent or self.finished:
            raise VolcengineASRError("ASR connection is not accepting audio")

    async def events(self) -> AsyncIterator[TranscriptEvent]:
        if self._ws is None:
            raise VolcengineASRError("ASR client is not connected")
        while not self.finished and not self._closed:
            try:
                frame = await self._ws.recv()
            except Exception as exc:
                if self._closed:
                    return
                raise VolcengineASRError("Volcengine ASR connection closed before final response") from exc
            if not isinstance(frame, bytes):
                raise VolcengineASRError("Expected binary ASR response")
            data, last_response = _parse(frame)
            result = data.get("result") or {}
            if not isinstance(result, dict):
                raise VolcengineASRError("Unexpected ASR result shape")
            base_language = _get_language(result, data.get("audio_info"), data)
            utterances = result.get("utterances") or []
            if not isinstance(utterances, list):
                raise VolcengineASRError("Unexpected ASR utterances shape")
            if utterances:
                for utterance in utterances:
                    if not isinstance(utterance, dict):
                        continue
                    text = utterance.get("text") or ""
                    if not isinstance(text, str) or not text.strip():
                        continue
                    start_ms = utterance.get("start_time")
                    end_ms = utterance.get("end_time")
                    key = (start_ms, end_ms, text)
                    final = utterance.get("definite") is True or last_response
                    speaker = utterance.get("speaker_id")
                    event = TranscriptEvent(
                        text=text,
                        final=final,
                        speaker_id=str(speaker) if speaker is not None else None,
                        language=_get_language(utterance) or base_language,
                        start_ms=start_ms if isinstance(start_ms, int) else None,
                        end_ms=end_ms if isinstance(end_ms, int) else None,
                    )
                    if final:
                        if key not in self._final_utterances:
                            self._final_utterances.add(key)
                            yield event
                        self._last_partial = None
                    elif key != self._last_partial:
                        self._last_partial = key
                        yield event
            else:
                text = result.get("text")
                if isinstance(text, str) and text.strip():
                    key = (None, None, text)
                    if last_response and key not in self._final_utterances:
                        self._final_utterances.add(key)
                        yield TranscriptEvent(text, True, language=base_language)
                    elif not last_response and key != self._last_partial:
                        self._last_partial = key
                        yield TranscriptEvent(text, False, language=base_language)
            if last_response:
                self.finished = True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None


async def _run_wav(path: str) -> None:
    key = os.environ.get("VOLC_API_KEY")
    if not key:
        raise ValueError("Set VOLC_API_KEY before the WAV connection test")
    with wave.open(path, "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
            raise ValueError("WAV must be uncompressed 16 kHz mono PCM16")
        client = VolcengineASRClient(key, os.environ.get("VOLC_RESOURCE_ID") or DEFAULT_RESOURCE_ID)
        await client.connect()

        async def print_events() -> None:
            async for event in client.events():
                print(json.dumps(event.__dict__, ensure_ascii=False), flush=True)

        reader = asyncio.create_task(print_events())
        try:
            while chunk := wav.readframes(3200):
                await client.send_pcm(chunk)
                await asyncio.sleep(len(chunk) / 32000)
            await client.finish()
            await asyncio.wait_for(reader, timeout=20)
        finally:
            reader.cancel()
            await client.close()


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Test Doubao streaming ASR with a 16 kHz mono PCM16 WAV")
    parser.add_argument("wav", help="Path to a local WAV file")
    args = parser.parse_args()
    asyncio.run(_run_wav(args.wav))
