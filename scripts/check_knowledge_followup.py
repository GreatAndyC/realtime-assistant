"""Check that a follow-up paper question uses meeting context and local evidence."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


async def check() -> dict:
    from src.agent import build_messages, stream_answer
    from src.models import Language, Utterance
    from src.storage import Storage
    from src.tool_agent import gather_agent_evidence

    with tempfile.TemporaryDirectory(prefix="meeting-knowledge-followup-") as directory:
        root = Path(directory)
        store = Storage(root / "meetings.db", root)
        store.create_or_resume("knowledge-followup", Language.ZH, "发言人")
        store.save_utterance(Utterance(
            utterance_id="prior-question", meeting_id="knowledge-followup",
            speaker="发言人", language=Language.ZH,
            text="小会，DeepSeek-V4 支持多长上下文？", timestamp_ms=1, seq=1,
        ))
        question = "小会，论文采用哪些方式提升长上下文效率？"
        steps: list[dict] = []

        async def emit(step: dict) -> None:
            steps.append({key: step.get(key) for key in ("tool", "status", "count")})

        snippets = await gather_agent_evidence(question, store, "knowledge-followup", emit)
        messages = build_messages(question, store.list_utterances("knowledge-followup"), [], snippets)
        answer = ""
        async for delta in stream_answer(messages):
            answer += delta
        store.close()

    result = {
        "prior_meeting_question_in_context": "DeepSeek-V4 支持多长上下文" in messages[-1]["content"],
        "tool_steps": steps,
        "local_sources": sum(item.get("source") == "local" for item in snippets),
        "answer_mentions_CSA": "CSA" in answer,
        "answer_mentions_HCA": "HCA" in answer,
        "answer_text": answer,
    }
    result["passed"] = bool(result["prior_meeting_question_in_context"] and
                            result["local_sources"] and result["answer_mentions_CSA"] and
                            result["answer_mentions_HCA"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    sys.path.insert(0, str(ROOT))
    report = asyncio.run(check())
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise AssertionError("Knowledge follow-up missed local evidence or paper facts")


if __name__ == "__main__":
    main()
