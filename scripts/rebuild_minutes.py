"""Rebuild minutes from a saved meeting after improving the summarizer."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting-id", required=True)
    parser.add_argument("--db-path", type=Path, default=Path("data/meetings.db"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--phase", choices=["partial", "final"], default="final")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--save", action="store_true", help="replace the saved minutes for this phase")
    parser.add_argument("--extractive", action="store_true", help="use deterministic raw-text extraction without an LLM call")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    sys.path.insert(0, str(ROOT))
    from src.minutes import MinutesService
    from src.storage import Storage
    from src.config import config

    if args.extractive:
        config.deepseek_api_key = ""

    store = Storage(args.db_path, args.data_dir)
    if store.get_meeting(args.meeting_id) is None:
        raise ValueError("meeting not found")
    minutes = asyncio.run(MinutesService(store).build(args.meeting_id, args.phase))
    if args.save:
        store.save_minutes(args.meeting_id, minutes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(minutes.model_dump_json(indent=2) + "\n")
    print(json.dumps({
        "meeting_id": args.meeting_id,
        "phase": args.phase,
        "discussion": len(minutes.discussion),
        "decisions": len(minutes.decisions),
        "action_items": len(minutes.action_items),
        "source_utterance_ids": len(minutes.source_utterance_ids),
        "saved": args.save,
        "extractive": args.extractive,
        "output": str(args.output),
    }, ensure_ascii=False))
    store.close()


if __name__ == "__main__":
    main()
