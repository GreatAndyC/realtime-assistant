"""Long-meeting minutes remain selective and cover the full transcript."""
from __future__ import annotations

import asyncio

from src.config import config
from src.minutes import MinutesService
from src.models import Language, MinutesData, PhaseSummary, Utterance
from src.storage import Storage


def test_extractive_fallback_selects_points_without_losing_source_coverage(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "deepseek_api_key", "")
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("long", Language.ZH, "说话人1")
    for index in range(24):
        text = "继续讨论一般事项"
        if index == 8:
            text = "决定周三发布蓝色方案"
        elif index == 18:
            text = "测试组负责完成回归测试"
        store.save_utterance(Utterance(
            utterance_id=f"u{index}", meeting_id="long", speaker="说话人1",
            language=Language.ZH, text=text, timestamp_ms=index, seq=index,
        ))

    minutes = asyncio.run(MinutesService(store).build("long", phase="final"))

    assert len(minutes.discussion) < 24
    assert any("周三发布蓝色方案" in item for item in minutes.decisions)
    assert any("回归测试" in item.action for item in minutes.action_items)
    assert minutes.source_utterance_ids == [f"u{index}" for index in range(24)]
    store.close()


def test_extractive_fallback_catches_explicit_assignments_and_needed_updates(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "deepseek_api_key", "")
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("actions", Language.ZH, "说话人1")
    for index, text in enumerate((
        "发布前由测试组完成回归测试。",
        "客服资料需要同步更新。",
        "如果测试未通过，发布时间要重新讨论。",
    )):
        store.save_utterance(Utterance(
            utterance_id=f"a{index}", meeting_id="actions", speaker="说话人1",
            language=Language.ZH, text=text, timestamp_ms=index, seq=index,
        ))
    minutes = asyncio.run(MinutesService(store).build("actions", phase="final"))
    assert len(minutes.action_items) == 2
    assert any("回归测试" in item.action for item in minutes.action_items)
    assert any("同步更新" in item.action for item in minutes.action_items)
    assigned = next(item for item in minutes.action_items if "回归测试" in item.action)
    assert assigned.person == "测试组"
    assert assigned.deadline == "发布前"
    store.close()


def test_extractive_minutes_do_not_turn_wake_question_into_decision(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "deepseek_api_key", "")
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("wake-question", Language.ZH, "说话人1")
    for index, text in enumerate((
        "我们决定下周三发布蓝色方案，先完成回归测试。",
        "小慧刚才决定什么时候发布",
    )):
        store.save_utterance(Utterance(
            utterance_id=f"w{index}", meeting_id="wake-question", speaker="说话人1",
            language=Language.ZH, text=text, timestamp_ms=index, seq=index,
        ))
    minutes = asyncio.run(MinutesService(store).build("wake-question", phase="final"))
    assert len(minutes.decisions) == 1
    assert "下周三发布蓝色方案" in minutes.decisions[0]
    assert any("回归测试" in item.action for item in minutes.action_items)
    assert minutes.action_items[0].action.startswith("先完成")
    assert minutes.action_items[0].deadline is None
    store.close()


def test_minutes_accepts_safe_scalar_fields_from_model(tmp_path):
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("scalar", Language.ZH, "说话人1")
    store.save_utterance(Utterance(
        utterance_id="u0", meeting_id="scalar", speaker="说话人1",
        language=Language.ZH, text="发布前由测试组完成回归测试。", timestamp_ms=0, seq=0,
    ))

    async def fake_stream(_messages):
        yield ('{"discussion":"发布计划","decisions":"周三发布蓝色方案",'
               '"action_items":{"person":"测试组","action":"完成回归测试",'
               '"deadline":"发布前"},"source_utterance_ids":[]}')

    minutes = asyncio.run(MinutesService(store, fake_stream).build("scalar", phase="final"))
    assert minutes.discussion == ["发布计划"]
    assert minutes.decisions == ["周三发布蓝色方案"]
    assert minutes.action_items[0].person == "测试组"
    assert minutes.source_utterance_ids == ["u0"]
    store.close()


def test_empty_model_minutes_fall_back_to_meeting_facts(tmp_path):
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("empty-model", Language.ZH, "说话人1")
    store.save_utterance(Utterance(
        utterance_id="u0", meeting_id="empty-model", speaker="说话人1",
        language=Language.ZH, text="决定周三发布蓝色方案。", timestamp_ms=0, seq=0,
    ))

    async def empty_stream(_messages):
        yield "{}"

    minutes = asyncio.run(MinutesService(store, empty_stream).build("empty-model", phase="final"))
    assert any("周三发布蓝色方案" in item for item in minutes.decisions)
    assert minutes.source_utterance_ids == ["u0"]
    store.close()


def test_final_minutes_restore_action_omitted_by_phase_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "deepseek_api_key", "")
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("summary-action", Language.ZH, "说话人1")
    store.save_utterance(Utterance(
        utterance_id="u0", meeting_id="summary-action", speaker="说话人1",
        language=Language.ZH, text="发布前由测试组完成回归测试。", timestamp_ms=0, seq=0,
    ))
    store.save_phase_summary(PhaseSummary(
        summary_id="p0", meeting_id="summary-action", created_at_ms=1,
        discussion=["发布计划"], decisions=[], action_items=[], source_utterance_ids=["u0"],
    ))
    minutes = asyncio.run(MinutesService(store).build("summary-action", phase="final"))
    assert any("回归测试" in item.action for item in minutes.action_items)
    assert minutes.source_utterance_ids == ["u0"]
    store.close()


def test_llm_reduce_keeps_source_ids_out_of_model_payload(tmp_path):
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("long", Language.ZH, "说话人1")
    for index in range(24):
        store.save_utterance(Utterance(
            utterance_id=f"u{index}", meeting_id="long", speaker="说话人1",
            language=Language.ZH, text="决定发布蓝色方案", timestamp_ms=index, seq=index,
        ))
    reduce_calls = []

    async def fake_stream(messages):
        content = messages[-1]["content"]
        if content.startswith("合并"):
            reduce_calls.append(content)
            assert "u0" not in content
        yield ('{"discussion":["确认发布计划"],"decisions":["发布蓝色方案"],'
               '"action_items":[],"source_utterance_ids":[]}')

    minutes = asyncio.run(MinutesService(store, fake_stream).build("long", phase="final"))
    assert reduce_calls
    assert minutes.source_utterance_ids == [f"u{index}" for index in range(24)]
    store.close()


def test_large_reduce_preserves_start_and_end_without_unbounded_model_call(tmp_path):
    store = Storage(tmp_path / "meetings.db", tmp_path)

    async def unexpected_model(_messages):
        raise AssertionError("large merge should be deterministic")
        yield ""

    parts = [MinutesData(
        phase="partial", discussion=[f"阶段 {index} 的讨论"],
        decisions=[f"决定 {index}"], action_items=[],
        source_utterance_ids=[f"u{index}"],
    ) for index in range(50)]
    minutes = asyncio.run(MinutesService(store, unexpected_model)._reduce(parts, "final"))
    assert len(minutes.discussion) == 12
    assert minutes.discussion[0] == "阶段 0 的讨论"
    assert minutes.discussion[-1] == "阶段 49 的讨论"
    assert len(minutes.decisions) == 50
    assert len(minutes.source_utterance_ids) == 50
    store.close()
