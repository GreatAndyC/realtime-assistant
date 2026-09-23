"""Agent must observe a tool result before selecting its next action."""
import asyncio
import json

from src.models import Language, Utterance
from src.storage import Storage
from src import tool_agent


def test_agent_uses_meeting_result_to_choose_second_tool(monkeypatch, tmp_path):
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("demo", Language.ZH, "说话人1")
    store.save_utterance(Utterance(utterance_id="u1", meeting_id="demo", speaker="说话人1",
                                   language=Language.ZH, text="决定明天发布产品", timestamp_ms=1, seq=1))
    model_inputs = []

    async def fake_model(messages, *, required):
        model_inputs.append(messages.copy())
        if len(model_inputs) == 1:
            assert required
            return {"content": None, "tool_calls": [{"id": "call1", "type": "function", "function":
                    {"name": "search_meeting", "arguments": json.dumps({"query": "发布产品"})}}]}
        if len(model_inputs) == 2:
            assert not required
            assert any(m.get("role") == "tool" and "明天发布产品" in m["content"] for m in messages)
            return {"content": None, "tool_calls": [{"id": "call2", "type": "function", "function":
                    {"name": "search_local_documents", "arguments": json.dumps({"query": "产品"})}}]}
        return {"content": "证据足够"}

    monkeypatch.setattr(tool_agent, "_model_step", fake_model)
    monkeypatch.setattr(tool_agent, "search_local", lambda query: [{"source": "local", "title": "产品资料", "snippet": "说明"}])
    events = []

    async def emit(event):
        events.append(event)

    results = asyncio.run(tool_agent.gather_agent_evidence("产品什么时候发布？", store, "demo", emit))
    assert [item["source"] for item in results] == ["meeting", "local"]
    assert [(item["tool"], item["status"]) for item in events] == [
        ("search_meeting", "started"), ("search_meeting", "done"),
        ("search_local_documents", "started"), ("search_local_documents", "done")]
    store.close()


def test_meeting_search_finds_decisions_before_recent_window(tmp_path):
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("long", Language.ZH, "说话人1")
    for index in range(205):
        store.save_utterance(Utterance(
            utterance_id=f"u{index}", meeting_id="long", speaker="说话人1",
            language=Language.ZH,
            text="决定采用蓝色方案" if index == 0 else "继续讨论时间安排",
            timestamp_ms=index, seq=index,
        ))
    results = tool_agent._meeting_results(store, "long", "蓝色方案")
    assert results[0]["snippet"].startswith("[u0]")
    store.close()
