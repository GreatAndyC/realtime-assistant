"""Browser-playable speech from Volcengine TTS 2.0 or macOS system voices."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import tempfile
import uuid
from pathlib import Path

import httpx


VOLC_TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
VOLC_TTS_RESOURCE_ID = "seed-tts-2.0"
DEFAULT_VOLC_VOICE_ZH = "zh_female_vv_uranus_bigtts"

class TtsError(RuntimeError):
    pass


async def _run(*args: str) -> tuple[int, bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as exc:
        raise TtsError(f"缺少语音工具：{args[0]}") from exc
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        process.kill()
        await process.communicate()
        raise
    return process.returncode or 0, stdout, stderr


def _volc_error(response: httpx.Response, api_key: str) -> str:
    try:
        payload = response.json()
        message = payload.get("header", {}).get("message") or payload.get("message") or ""
    except (ValueError, AttributeError):
        message = ""
    message = str(message).replace(api_key, "[REDACTED]")[:200]
    if response.status_code in (401, 403):
        return (f"火山语音合成 2.0 未授权（HTTP {response.status_code}）。"
                f"请在豆包语音控制台开通“语音合成2.0”并确认 API Key 所属项目。{message}")
    return f"火山语音合成请求失败（HTTP {response.status_code}）{': ' + message if message else ''}"


def _decode_volc_audio(response: httpx.Response, api_key: str) -> bytes:
    audio = bytearray()
    for line in response.text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise TtsError("火山语音合成返回了无法解析的结果") from exc
        header = event.get("header") or {}
        code = event.get("code", header.get("code", 0))
        if code not in (0, 20000000):
            message = str(event.get("message") or header.get("message") or "未知错误")
            raise TtsError(f"火山语音合成失败：{message.replace(api_key, '[REDACTED]')[:200]}")
        chunk = event.get("data")
        if chunk:
            try:
                audio.extend(base64.b64decode(chunk, validate=True))
            except (ValueError, binascii.Error) as exc:
                raise TtsError("火山语音合成返回了损坏的音频") from exc
    if not audio:
        raise TtsError("火山语音合成没有返回音频")
    return bytes(audio)


async def _synthesize_volc(text: str, language: str) -> bytes:
    api_key = os.environ.get("VOLC_API_KEY", "")
    if not api_key:
        raise TtsError("使用火山语音合成需配置 VOLC_API_KEY")
    voice = os.environ.get("VOLC_TTS_VOICE_ZH") or DEFAULT_VOLC_VOICE_ZH
    if language == "yue":
        voice = os.environ.get("VOLC_TTS_VOICE_YUE", "")
        if not voice:
            return await _synthesize_system(text, language)
    body = {
        "req_params": {
            "text": text[:4000],
            "speaker": voice,
            "audio_params": {"format": "mp3", "sample_rate": 24000},
            "additions": json.dumps({"disable_markdown_filter": True}),
        }
    }
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": VOLC_TTS_RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(VOLC_TTS_URL, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise TtsError(f"火山语音合成连接失败：{type(exc).__name__}") from exc
    if response.status_code != 200:
        raise TtsError(_volc_error(response, api_key))
    return _decode_volc_audio(response, api_key)


async def _synthesize_system(text: str, language: str) -> bytes:
    """Render with macOS say, retaining the existing offline fallback."""
    voice = "Sinji" if language == "yue" else "Tingting"
    with tempfile.TemporaryDirectory(prefix="meeting-tts-") as directory:
        source = Path(directory) / "speech.txt"
        aiff = Path(directory) / "speech.aiff"
        source.write_text(text[:4000], encoding="utf-8")
        returncode, _, stderr = await _run("say", "-v", voice, "-o", str(aiff), "-f", str(source))
        if returncode:
            raise TtsError(f"系统语音合成失败：{stderr.decode(errors='replace')[:300]}")
        returncode, mp3, stderr = await _run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(aiff),
            "-codec:a", "libmp3lame", "-b:a", "96k", "-f", "mp3", "pipe:1")
        if returncode or not mp3:
            raise TtsError(f"音频转换失败：{stderr.decode(errors='replace')[:300]}")
        return mp3


async def synthesize_speech(text: str, language: str = "zh") -> bytes:
    """Generate MP3 using the configured voice provider."""
    if not text.strip():
        raise TtsError("不能合成空白语音")
    language = getattr(language, "value", language)
    provider = os.environ.get("TTS_PROVIDER", "system").lower()
    if provider == "volcengine":
        return await _synthesize_volc(text, language)
    if provider == "system":
        return await _synthesize_system(text, language)
    raise TtsError(f"不支持的 TTS_PROVIDER：{provider}")
