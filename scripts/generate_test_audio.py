"""Generate reproducible Mandarin, Cantonese, and 30-minute meeting audio."""
from __future__ import annotations

import argparse
import subprocess
import tempfile
import wave
from pathlib import Path


RATE = 16000
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "audio"
SILENCE = b"\0\0" * (RATE * 2)

ZH_LINES = [
    ("Tingting", "今天讨论云舟预约的上线时间。"),
    ("Tingting", "我们决定下周三发布蓝色方案，先完成回归测试。"),
    ("Tingting", "小会，刚才决定什么时候发布？"),
]
YUE_LINES = [
    ("Sinji", "今日我哋討論雲舟預約嘅發布時間。"),
    ("Sinji", "我哋決定下個禮拜三發布藍色方案。"),
    ("Sinji", "小會，頭先決定幾時發布？"),
]
KNOWLEDGE_LINES = [("Tingting", "小会，DeepSeek V四支持多长的上下文？")]
WEB_LINES = [
    ("Tingting", "小会，请联网搜索机器学习是什么意思？"),
    ("Tingting", "我们继续讨论测试计划。"),
]
LONG_LINES = [
    ("Tingting", "今天先确认预约系统的发布计划。"),
    ("Eddy (Chinese (China mainland))", "测试组已经完成主要流程，仍需检查取消预约功能。"),
    ("Tingting", "我们决定周三发布蓝色方案。"),
    ("Eddy (Chinese (China mainland))", "发布前由测试组完成回归测试。"),
    ("Tingting", "客服资料需要同步更新。"),
    ("Eddy (Chinese (China mainland))", "如果测试未通过，发布时间要重新讨论。"),
    ("Sinji", "我哋仲要確認廣東話用戶嘅預約流程。"),
    ("Sinji", "我哋決定下個禮拜三再檢查一次。"),
]


def _speech(voice: str, text: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="meeting-fixture-") as directory:
        aiff = Path(directory) / "speech.aiff"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(aiff),
             "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
            check=True, capture_output=True,
        )
        return result.stdout


def _write_wav(path: Path, segments: list[bytes]) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        for segment in segments:
            output.writeframes(segment)


def generate(*, short_only: bool = False) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    unique = {entry for entry in ZH_LINES + YUE_LINES + KNOWLEDGE_LINES + WEB_LINES + LONG_LINES}
    spoken = {entry: _speech(*entry) for entry in sorted(unique)}
    _write_wav(OUTPUT / "mandarin.wav", [
        chunk for entry in ZH_LINES for chunk in (spoken[entry], SILENCE)
    ])
    _write_wav(OUTPUT / "cantonese.wav", [
        chunk for entry in YUE_LINES for chunk in (spoken[entry], SILENCE)
    ])
    _write_wav(OUTPUT / "knowledge.wav", [
        chunk for entry in KNOWLEDGE_LINES for chunk in (spoken[entry], SILENCE)
    ])
    _write_wav(OUTPUT / "web.wav", [
        chunk for entry in WEB_LINES for chunk in (spoken[entry], SILENCE)
    ])
    if short_only:
        return

    target_bytes = RATE * 2 * 1800
    with tempfile.TemporaryDirectory(prefix="meeting-long-") as directory:
        long_wav = Path(directory) / "meeting-30m.wav"
        written = 0
        with wave.open(str(long_wav), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(RATE)
            while written < target_bytes:
                for entry in LONG_LINES:
                    for chunk in (spoken[entry], SILENCE):
                        remaining = target_bytes - written
                        if not remaining:
                            break
                        part = chunk[:remaining]
                        output.writeframes(part)
                        written += len(part)
                    if written == target_bytes:
                        break
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(long_wav),
            "-c:a", "libopus", "-b:a", "24k", str(OUTPUT / "meeting-30m.ogg"),
        ], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--short-only", action="store_true",
                        help="leave the 30-minute asset untouched")
    arguments = parser.parse_args()
    generate(short_only=arguments.short_only)
