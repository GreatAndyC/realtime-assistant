"""Replay a test recording through the real meeting WebSocket at recording speed."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import struct
import subprocess
import time
from collections import Counter
from pathlib import Path

import httpx
import websockets


RATE = 16000
FRAME_BYTES = RATE * 2 // 5


def audio_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


async def replay(args: argparse.Namespace) -> dict:
    expected_duration = audio_duration(args.audio)
    counts: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    states: list[str] = []
    errors: list[str] = []
    transcript: list[str] = []
    tool_steps: list[dict] = []
    search_sources: Counter[str] = Counter()
    timeline: list[dict] = []
    final_minutes = None
    tts_bytes = 0
    acked = -1
    ready = asyncio.Event()
    ended = asyncio.Event()
    answered_and_listening = asyncio.Event()
    answer_text = ""
    started = time.monotonic()
    uri = args.base_url.replace("http://", "ws://").replace("https://", "wss://")
    uri = uri.rstrip("/") + f"/ws/meetings/{args.meeting_id}"

    async with websockets.connect(uri, max_size=16 * 1024 * 1024, ping_timeout=None) as socket:
        async def receive() -> None:
            nonlocal final_minutes, tts_bytes, acked, answer_text
            try:
                async for raw in socket:
                    event = json.loads(raw)
                    kind = event.get("type", "")
                    data = event.get("data") or {}
                    counts[kind] += 1
                    if kind in {"asr.final", "agent.step", "llm.done", "tts.audio", "minutes.ready", "error"}:
                        timeline.append({"second": round(time.monotonic() - started, 2),
                                         "type": kind, "status": data.get("status") or data.get("phase"),
                                         "seq": data.get("seq")})
                    if kind == "ack":
                        acked = max(acked, data.get("seq", -1))
                        if "sample_offset" in data:
                            ready.set()
                    elif kind == "asr.final":
                        languages[data.get("language", "unknown")] += 1
                        transcript.append(data.get("text", ""))
                    elif kind == "agent.state":
                        states.append(data.get("state", ""))
                        if data.get("state") == "LISTENING" and "ANSWERING" in states:
                            answered_and_listening.set()
                        if data.get("state") == "ENDED":
                            ended.set()
                    elif kind == "llm.done":
                        answer_text = data.get("full_text", "")
                    elif kind == "agent.step":
                        tool_steps.append({"tool": data.get("tool"), "status": data.get("status"),
                                           "count": data.get("count")})
                    elif kind == "search.result":
                        search_sources.update(item.get("source", "unknown")
                                              for item in data.get("snippets", []))
                    elif kind == "tts.audio":
                        tts_bytes += len(base64.b64decode(data.get("audio_b64", "")))
                    elif kind == "minutes.ready" and data.get("phase") == "final":
                        final_minutes = data
                    elif kind == "error":
                        errors.append(data.get("code", "UNKNOWN"))
            except websockets.ConnectionClosed:
                pass

        receiver = asyncio.create_task(receive())
        await socket.send(json.dumps({"type": "config", "language": args.language}))
        try:
            await asyncio.wait_for(ready.wait(), timeout=20)
        except asyncio.TimeoutError as exc:
            receiver.cancel()
            raise RuntimeError(f"meeting handshake failed: {errors}") from exc

        decoder = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(args.audio),
            "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        sent_samples = 0
        seq = 0
        partial_sent = False
        next_send = time.monotonic()
        try:
            while True:
                pcm = await decoder.stdout.readexactly(FRAME_BYTES)
                await socket.send(struct.pack("<IQ", seq, sent_samples) + pcm)
                sent_samples += len(pcm) // 2
                seq += 1
                if args.partial_at and not partial_sent and sent_samples >= args.partial_at * RATE:
                    await socket.send(json.dumps({"type": "partial_minutes"}))
                    partial_sent = True
                next_send += len(pcm) / (RATE * 2)
                if args.realtime:
                    await asyncio.sleep(max(0, next_send - time.monotonic()))
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                pcm = exc.partial[:len(exc.partial) // 2 * 2]
                if pcm:
                    await socket.send(struct.pack("<IQ", seq, sent_samples) + pcm)
                    sent_samples += len(pcm) // 2
                    seq += 1
                    if args.realtime:
                        next_send += len(pcm) / (RATE * 2)
                        await asyncio.sleep(max(0, next_send - time.monotonic()))
        finally:
            await decoder.wait()
        if decoder.returncode:
            raise RuntimeError(f"ffmpeg decoder exited with {decoder.returncode}")
        if sent_samples / RATE < expected_duration - 1:
            raise RuntimeError(
                f"decoded only {sent_samples / RATE:.2f}s of {expected_duration:.2f}s; "
                "the audio file may have changed during replay"
            )
        await socket.send(json.dumps({"type": "flush"}))
        if args.expect_answer:
            await asyncio.wait_for(answered_and_listening.wait(), timeout=args.finish_timeout)
        await socket.send(json.dumps({"type": "end_meeting"}))
        await asyncio.wait_for(ended.wait(), timeout=args.finish_timeout)
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)

    elapsed = time.monotonic() - started
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(args.base_url.rstrip("/") + f"/api/meetings/{args.meeting_id}")
        response.raise_for_status()
        detail = response.json()
    saved = detail["utterances"]
    report = {
        "meeting_id": args.meeting_id,
        "audio": str(args.audio),
        "audio_seconds": round(sent_samples / RATE, 2),
        "asset_seconds": round(expected_duration, 2),
        "wall_seconds": round(elapsed, 2),
        "frames_sent": seq,
        "frames_acked": acked + 1,
        "events": dict(counts),
        "languages": dict(languages),
        "states": states,
        "error_codes": errors,
        "saved_utterances": len(saved),
        "first_final_seq": min((item["seq"] for item in saved), default=None),
        "last_final_seq": max((item["seq"] for item in saved), default=None),
        "tts_audio_bytes": tts_bytes,
        "answer_text": answer_text,
        "tool_steps": tool_steps,
        "search_sources": dict(search_sources),
        "timeline": timeline,
        "partial_minutes_received": counts["minutes.ready"] - bool(final_minutes),
        "final_minutes": final_minutes is not None,
        "final_minutes_source_ids": len(final_minutes["source_utterance_ids"]) if final_minutes else 0,
        "transcript_first": transcript[:2],
        "transcript_last": transcript[-2:],
        "passed": False,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.expect_language and languages[args.expect_language] == 0:
        raise AssertionError(f"No {args.expect_language} final subtitles: {report}")
    if acked + 1 != seq or not counts["asr.partial"] or not counts["asr.final"]:
        raise AssertionError(f"Audio or streaming subtitles incomplete: {report}")
    if args.expect_answer and not (counts["llm.delta"] and counts["llm.done"] and tts_bytes):
        raise AssertionError(f"Missing LLM or TTS answer: {report}")
    if args.expect_answer and counts["llm.done"] != 1:
        raise AssertionError(f"Unexpected number of answers: {report}")
    if not args.expect_answer and counts["llm.done"]:
        raise AssertionError(f"Unprompted assistant answer: {report}")
    if args.expect_answer and not answered_and_listening.is_set():
        raise AssertionError(f"Assistant did not return to listening: {report}")
    if args.expect_answer_text and args.expect_answer_text not in answer_text:
        raise AssertionError(f"Answer missed expected fact: {report}")
    if args.expect_source and search_sources[args.expect_source] == 0:
        raise AssertionError(f"Expected search source missing: {report}")
    if args.expect_transcript and not any(args.expect_transcript in text for text in transcript):
        raise AssertionError(f"Expected transcript text missing: {report}")
    if args.partial_at and report["partial_minutes_received"] < 1:
        raise AssertionError(f"No partial minutes: {report}")
    if not final_minutes or len(saved) != report["final_minutes_source_ids"]:
        raise AssertionError(f"Final minutes do not cover the saved transcript: {report}")
    if args.min_seconds and sent_samples / RATE < args.min_seconds:
        raise AssertionError(f"Recording too short: {report}")
    if args.min_seconds >= 1800 and report["last_final_seq"] < seq * 0.8:
        raise AssertionError(f"Final subtitles stopped before the end: {report}")
    if errors:
        raise AssertionError(f"Meeting reported errors: {report}")
    if args.minutes_example and final_minutes:
        args.minutes_example.parent.mkdir(parents=True, exist_ok=True)
        args.minutes_example.write_text(json.dumps(final_minutes, ensure_ascii=False, indent=2) + "\n")
    report["passed"] = True
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--meeting-id", required=True)
    parser.add_argument("--language", choices=["auto", "zh", "yue"], default="auto")
    parser.add_argument("--realtime", action="store_true", default=True)
    parser.add_argument("--fast", action="store_false", dest="realtime")
    parser.add_argument("--partial-at", type=float, default=0)
    parser.add_argument("--expect-language", choices=["zh", "yue"])
    parser.add_argument("--expect-answer", action="store_true")
    parser.add_argument("--expect-answer-text")
    parser.add_argument("--expect-source", choices=["meeting", "local", "web"])
    parser.add_argument("--expect-transcript")
    parser.add_argument("--min-seconds", type=float, default=0)
    parser.add_argument("--finish-timeout", type=float, default=300)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--minutes-example", type=Path)
    args = parser.parse_args()
    result = asyncio.run(replay(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
