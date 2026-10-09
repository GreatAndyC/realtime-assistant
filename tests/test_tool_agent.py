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


def test_web_choice_uses_sufficient_local_document_first(monkeypatch, tmp_path):
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("demo", Language.ZH, "说话人1")
    calls = []

    async def model(messages, *, required):
        if required:
            return {"content": None, "tool_calls": [{"id": "call1", "type": "function",
                    "function": {"name": "search_web", "arguments": json.dumps({"query": "DeepSeek V4 上下文长度"})}}]}
        return {"content": "资料足够"}

    async def forbidden_web(query):
        raise AssertionError("sufficient local evidence must not trigger web search")

    monkeypatch.setattr(tool_agent, "_model_step", model)
    monkeypatch.setattr(tool_agent, "search_local", lambda query: [
        {"source": "local", "title": "论文摘要", "snippet": "支持一百万 token 上下文", "score": 0.9}])
    monkeypatch.setattr(tool_agent, "search_web", forbidden_web)

    async def emit(event):
        calls.append(event)

    results = asyncio.run(tool_agent.gather_agent_evidence(
        "DeepSeek V4 支持多长上下文？", store, "demo", emit))
    assert results[0]["source"] == "local"
    assert [item["tool"] for item in calls] == ["search_local_documents"] * 2
    store.close()


def test_repeated_tool_request_reuses_result(monkeypatch, tmp_path):
    store = Storage(tmp_path / "meetings.db", tmp_path)
    store.create_or_resume("demo", Language.ZH, "说话人1")
    model_calls = 0
    search_calls = 0
    events = []

    async def model(messages, *, required):
        nonlocal model_calls
        model_calls += 1
        if model_calls <= 2:
            return {"content": None, "tool_calls": [{"id": f"call{model_calls}",
                    "type": "function", "function": {"name": "search_local_documents",
                    "arguments": json.dumps({"query": "发布计划"})}}]}
        return {"content": "足够"}

    def local(query):
        nonlocal search_calls
        search_calls += 1
        return [{"source": "local", "title": "计划", "snippet": "周三发布", "score": 0.8}]

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(tool_agent, "_model_step", model)
    monkeypatch.setattr(tool_agent, "search_local", local)
    found = asyncio.run(tool_agent.gather_agent_evidence("发布计划", store, "demo", emit))
    assert search_calls == 1
    assert len(found) == 1
    assert [e.get("cached") for e in events if e["status"] == "done"] == [False, True]
    store.close()
