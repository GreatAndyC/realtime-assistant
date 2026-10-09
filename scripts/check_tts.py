"""Generate a short reply and verify that FFprobe can decode its MP3."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=["zh", "yue"], default="zh")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    sys.path.insert(0, str(ROOT))
    from src.tts import synthesize_speech

    provider = os.environ.get("TTS_PROVIDER", "system").lower()
    if args.language == "yue" and provider == "volcengine" and not os.environ.get("VOLC_TTS_VOICE_YUE"):
        provider = "system"
    sentence = "你好，我是小会。" if args.language == "zh" else "你好，我係小會。"
    audio = asyncio.run(synthesize_speech(sentence, args.language))
    with tempfile.TemporaryDirectory(prefix="meeting-tts-check-") as directory:
        path = Path(directory) / "reply.mp3"
        path.write_bytes(audio)
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_name:format=duration",
            "-of", "json", str(path),
        ], check=True, capture_output=True, text=True)
    probe = json.loads(result.stdout)
    codec = probe.get("streams", [{}])[0].get("codec_name")
    duration = float(probe.get("format", {}).get("duration", 0))
    if codec != "mp3" or duration <= 0:
        raise RuntimeError("语音输出不是可播放的 MP3")
    print(json.dumps({"language": args.language, "provider": provider,
                      "codec": codec, "duration_seconds": duration,
                      "audio_bytes": len(audio)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
