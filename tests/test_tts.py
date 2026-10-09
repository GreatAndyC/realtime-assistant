"""Volcengine voice output preserves MP3 chunks and reports missing access."""
from __future__ import annotations

import asyncio
import base64
import json
import platform
import shutil
import subprocess

import httpx
import pytest

from src import tts


@pytest.mark.skipif(platform.system() != "Darwin" or not shutil.which("ffprobe"),
                    reason="requires macOS voices and FFmpeg")
def test_system_tts_produces_playable_mp3(monkeypatch, tmp_path):
    monkeypatch.setenv("TTS_PROVIDER", "system")
    audio = asyncio.run(tts.synthesize_speech("你好，小会。"))
    path = tmp_path / "speech.mp3"
    path.write_bytes(audio)
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], capture_output=True, text=True, check=True)
    assert float(result.stdout.strip()) > 0


def test_volc_tts_collects_audio_chunks(monkeypatch):
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        audio = [b"ID3first", b"second"]
        lines = [json.dumps({"code": 0, "data": base64.b64encode(part).decode()}) for part in audio]
        lines.append(json.dumps({"code": 20000000, "message": "OK"}))
        return httpx.Response(200, text="\n".join(lines))

    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(tts.httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport))
    monkeypatch.setenv("VOLC_API_KEY", "private-test-key")
    monkeypatch.setenv("TTS_PROVIDER", "volcengine")

    result = asyncio.run(tts.synthesize_speech("你好，**小会**。"))

    assert result == b"ID3firstsecond"
    assert requests[0].headers["X-Api-Resource-Id"] == "seed-tts-2.0"
    payload = json.loads(requests[0].content)
    assert payload["req_params"]["speaker"] == tts.DEFAULT_VOLC_VOICE_ZH
    assert json.loads(payload["req_params"]["additions"])["disable_markdown_filter"] is True


def test_volc_tts_403_identifies_missing_service_without_key(monkeypatch):
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"header": {
            "message": "[resource_id=volc.seedtts.default] requested resource not granted"
        }})

    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(tts.httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport))
    monkeypatch.setenv("VOLC_API_KEY", "private-test-key")
    monkeypatch.setenv("TTS_PROVIDER", "volcengine")

    with pytest.raises(tts.TtsError, match="语音合成2.0") as error:
        asyncio.run(tts.synthesize_speech("你好"))

    assert "private-test-key" not in str(error.value)
