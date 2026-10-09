"""Resume an interrupted meeting and request its final minutes without resending audio."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path

import httpx
import websockets


async def finalize(args: argparse.Namespace) -> dict:
    uri = args.base_url.replace("http://", "ws://").replace("https://", "wss://")
    uri = uri.rstrip("/") + f"/ws/meetings/{args.meeting_id}"
    events: Counter[str] = Counter()
    errors: list[str] = []
    states: list[str] = []
    ready = False
    ending_sent = False
    minutes = None
    checkpoint = None
    started = time.monotonic()
    async with websockets.connect(uri, max_size=16 * 1024 * 1024, ping_timeout=None) as socket:
        await socket.send(json.dumps({"type": "config", "language": args.language}))
        async with asyncio.timeout(args.timeout):
            async for raw in socket:
                event = json.loads(raw)
                kind = event.get("type", "")
                data = event.get("data") or {}
                events[kind] += 1
                if kind == "error":
                    errors.append(data.get("code", "UNKNOWN"))
                    if not ready:
                        raise RuntimeError(f"meeting handshake failed: {errors}")
                elif kind == "ack" and "sample_offset" in data and not ready:
                    ready = True
                    checkpoint = {"seq": data["seq"], "sample_offset": data["sample_offset"]}
                    await socket.send(json.dumps({"type": "end_meeting"}))
                    ending_sent = True
                elif kind == "minutes.ready" and data.get("phase") == "final":
                    minutes = data
                elif kind == "agent.state":
                    states.append(data.get("state", ""))
                    if data.get("state") == "ENDED":
                        break
    if not ready or not ending_sent or minutes is None or not states or states[-1] != "ENDED":
        raise AssertionError("meeting was not finalized")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(args.base_url.rstrip("/") + f"/api/meetings/{args.meeting_id}")
        response.raise_for_status()
        detail = response.json()
    saved = detail["utterances"]
    expected_ids = {item["utterance_id"] for item in saved}
    actual_ids = set(minutes["source_utterance_ids"])
    result = {
        "meeting_id": args.meeting_id,
        "resume_seconds": round(time.monotonic() - started, 2),
        "audio_seconds": round(checkpoint["sample_offset"] / 16000, 2),
        "frames_stored": checkpoint["seq"] + 1,
        "saved_utterances": len(saved),
        "last_final_seq": max((item["seq"] for item in saved), default=None),
        "history_final_events": events["asr.final"],
        "final_minutes_source_ids": len(actual_ids),
        "states": states,
        "error_codes": errors,
        "passed": not errors and actual_ids == expected_ids and detail["meeting"]["state"] == "ENDED",
    }
    if args.minutes_example:
        args.minutes_example.parent.mkdir(parents=True, exist_ok=True)
        args.minutes_example.write_text(json.dumps(minutes, ensure_ascii=False, indent=2) + "\n")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if not result["passed"]:
        raise AssertionError(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--language", choices=["auto", "zh", "yue"], default="auto")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--minutes-example", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(finalize(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
