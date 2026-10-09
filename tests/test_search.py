"""Local evidence takes priority; web failures retain a usable fallback."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest

from src import search


def test_local_knowledge_retrieval(tmp_path):
    (tmp_path / "paper.md").write_text("DeepSeek-V4 支持一百万 token 上下文。")
    results = search.search_local("DeepSeek-V4 上下文多长", knowledge_dir=tmp_path)
    assert results[0]["title"] == "paper.md"
    assert "一百万 token" in results[0]["snippet"]


def test_wikipedia_fallback_after_general_web_failure(monkeypatch):
    class FailedDDGS:
        def __enter__(self):
            raise RuntimeError("ddgs unavailable")

        def __exit__(self, *_args):
            pass

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FailedDDGS))

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "zh.wikipedia.org"
        assert request.url.params["srsearch"] == "机器学习"
        return httpx.Response(200, json={"query": {"search": [
            {"title": "机器学习", "snippet": "一种<b>学习</b>方法"},
        ]}})

    original = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(search.httpx, "AsyncClient",
                        lambda **kwargs: original(transport=transport))
    results = asyncio.run(search.search_web("请联网搜索机器学习是什么意思？"))
    assert results[0]["snippet"] == "一种学习方法"
    assert unquote(results[0]["url"]) == "https://zh.wikipedia.org/wiki/机器学习"


def test_current_web_failure_does_not_return_old_encyclopedia(monkeypatch):
    class FailedDDGS:
        def __enter__(self):
            raise RuntimeError("ddgs unavailable")

        def __exit__(self, *_args):
            pass

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=FailedDDGS))
    with pytest.raises(RuntimeError, match="实时网页搜索暂不可用"):
        asyncio.run(search.search_web("今天的天气"))
