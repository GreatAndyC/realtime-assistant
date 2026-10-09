"""Small, bounded document retrieval and optional asynchronous web search."""
from __future__ import annotations

import asyncio
import html
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import config


_WORDS = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*|[\u3400-\u9fff]+")
_STOP = {"什么", "怎么", "如何", "一下", "请问", "关于", "这个", "那个", "我们", "你们", "他们", "哪些", "是否", "多少", "可以", "帮我", "介绍", "解释", "资料", "文档", "会议", "小会"}
_MAX_FILE_BYTES = 12 * 1024 * 1024
_MAX_PDF_PAGES = 100
_CHUNK_SIZE = 1100
_CHUNK_OVERLAP = 160


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for match in _WORDS.finditer(text.lower()):
        token = match.group()
        if token in _STOP:
            continue
        if "\u3400" <= token[0] <= "\u9fff":
            terms.update(token[i:i + 2] for i in range(len(token) - 1))
            if len(token) == 1:
                terms.add(token)
        elif len(token) > 1:
            terms.add(token)
            terms.update(part for part in re.split(r"[-_]", token) if len(part) > 1)
    return terms


def _chunks(text: str):
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return
    step = _CHUNK_SIZE - _CHUNK_OVERLAP
    for start in range(0, len(text), step):
        chunk = text[start:start + _CHUNK_SIZE].strip()
        if chunk:
            yield chunk
        if start + _CHUNK_SIZE >= len(text):
            break


def _score(query_terms: set[str], content: str, title: str) -> float:
    if not query_terms:
        return 0.0
    body_terms = _terms(content)
    title_terms = _terms(title)
    matched = query_terms & (body_terms | title_terms)
    return (len(matched) + 0.5 * len(matched & title_terms)) / len(query_terms)


def search_local(query: str, *, knowledge_dir: Path | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Search Markdown, TXT and text PDFs within the configured knowledge directory.

    Missing directories and unreadable individual files are skipped. Symlinks are
    never followed, so an untrusted knowledge file cannot escape this directory.
    Each result has source/title/snippet/path/page/score keys.
    """
    root = Path(knowledge_dir or config.knowledge_dir)
    if not root.is_dir() or root.is_symlink():
        return []
    terms = _terms(query)
    if not terms:
        return []
    results: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {".md", ".txt", ".pdf"}:
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            pages: list[tuple[int | None, str]]
            if path.suffix.lower() == ".pdf":
                from pypdf import PdfReader

                reader = PdfReader(str(path))
                pages = [(number, page.extract_text() or "") for number, page in enumerate(reader.pages[:_MAX_PDF_PAGES], 1)]
            else:
                pages = [(None, path.read_text(encoding="utf-8", errors="replace"))]
            for page_number, page_text in pages:
                for snippet in _chunks(page_text):
                    score = _score(terms, snippet, path.stem)
                    if score > 0:
                        results.append({"source": "local", "title": path.name, "snippet": snippet,
                                        "path": str(path), "page": page_number, "score": round(score, 3)})
        except (OSError, ValueError, RuntimeError, ImportError):
            continue
    results.sort(key=lambda result: result["score"], reverse=True)
    return results[:max(0, limit if limit is not None else config.max_search_snippets)]


async def search_web(query: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Search the web, with Wikipedia as a fallback for non-current facts."""
    count = limit if limit is not None else config.web_search_max_results

    def run() -> list[dict[str, Any]]:
        from ddgs import DDGS

        with DDGS() as client:
            raw = list(client.text(query, max_results=count))
        return [{"source": "web", "title": str(item.get("title", "")),
                 "snippet": str(item.get("body", ""))[:_CHUNK_SIZE],
                 "url": str(item.get("href", "")), "score": None}
                for item in raw if item.get("body") or item.get("title")]

    try:
        results = await asyncio.wait_for(asyncio.to_thread(run),
                                         timeout=max(1.0, config.web_search_timeout / 2))
        if results:
            return results
    except Exception:
        pass
    if re.search(r"最新|今天|当前|现在|实时|新闻|recent|latest|today", query, re.I):
        raise RuntimeError("实时网页搜索暂不可用，请稍后重试")
    encyclopedia_query = re.sub(
        r"请联网搜索|請聯網搜尋|联网搜索|聯網搜尋|请搜索|請搜尋|是什么意思|是什麼意思|"
        r"是什么|是什麼|的定义|的定義|解释|解釋|[？?。]",
        "", query, flags=re.I,
    ).strip() or query
    site = "zh" if re.search(r"[\u3400-\u9fff]", encyclopedia_query) else "en"
    endpoint = f"https://{site}.wikipedia.org/w/api.php"
    async with httpx.AsyncClient(timeout=max(1.0, config.web_search_timeout / 2)) as client:
        response = await client.get(
            endpoint,
            params={"action": "query", "list": "search", "srsearch": encyclopedia_query,
                    "srlimit": count, "format": "json"},
            headers={"User-Agent": "RealtimeMeetingAssistant/1.0 (public knowledge search)"},
        )
        response.raise_for_status()
    found = response.json().get("query", {}).get("search", [])
    results = [{"source": "web", "title": str(item.get("title", "")),
                "snippet": html.unescape(re.sub(r"<[^>]+>", "", item.get("snippet", "")))[:_CHUNK_SIZE],
                "url": f"https://{site}.wikipedia.org/wiki/{quote(str(item.get('title', '')).replace(' ', '_'))}",
                "score": None}
               for item in found if item.get("title")]
    if not results:
        raise RuntimeError("网页搜索未返回结果")
    return results


def needs_web_search(query: str, local_results: list[dict[str, Any]]) -> bool:
    """Prefer local evidence unless the question explicitly asks for current web data."""
    if re.search(r"最新|今天|当前|现在|实时|新闻|网上|网页|联网|搜索网络|recent|latest|today", query, re.I):
        return True
    return not local_results or (local_results[0].get("score") or 0) < 0.35


async def gather_evidence(query: str, *, knowledge_dir: Path | None = None,
                          allow_web: bool = True) -> list[dict[str, Any]]:
    """Search local first, then web when local evidence is weak or freshness matters."""
    local = await asyncio.to_thread(search_local, query, knowledge_dir=knowledge_dir)
    if not allow_web or not needs_web_search(query, local):
        return local
    web = await search_web(query)
    return (local + web)[:config.max_search_snippets]
