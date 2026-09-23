"""macOS system TTS rendered to browser-playable MP3 using ffmpeg."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

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


async def synthesize_speech(text: str, language: str = "zh") -> bytes:
    """Generate MP3 bytes. Raises TtsError when say or ffmpeg fails."""
    if not text.strip():
        raise TtsError("不能合成空白语音")
    voice = "Sinji" if getattr(language, "value", language) == "yue" else "Tingting"
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
