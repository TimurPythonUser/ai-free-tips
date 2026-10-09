"""Поиск тем для постов через Exa MCP-сервер (запуск exa-mcp-server по stdio).
Возвращает (сырой текст ответа, список источников с датами) и логирует MCP-обмен."""
from __future__ import annotations

import json

from . import config
from .logbus import bus
from .mcp_client import MCPStdioClient


def _parse_exa(text: str) -> list[dict]:
    """Достаём из ответа Exa статьи (title/url/date). Exa отдаёт текст вида
    'Title: … / URL: … / Published: …', записи разделены '---'. JSON — запасной вариант."""
    text = (text or "").strip()

    # запасной вариант: вдруг какая-то версия вернёт JSON
    try:
        data = json.loads(text)
        results = data.get("results") if isinstance(data, dict) else (data if isinstance(data, list) else [])
        out = []
        for r in results or []:
            if isinstance(r, dict) and (r.get("url") or r.get("id")):
                date = (r.get("publishedDate") or r.get("published_date") or "")[:10] or None
                out.append({"url": r.get("url") or r.get("id"), "title": r.get("title"), "date": date})
        if out:
            return out
    except Exception:
        pass

    # основной вариант: текстовый формат Exa
    out: list[dict] = []
    cur: dict = {}

    def flush():
        if cur.get("url"):
            out.append({"url": cur["url"], "title": cur.get("title"), "date": cur.get("date")})

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Title:"):
            if cur:
                flush()
            cur = {"title": line[len("Title:"):].strip()}
        elif line.startswith("URL:"):
            cur["url"] = line[len("URL:"):].strip()
        elif line.startswith("Published:"):
            v = line[len("Published:"):].strip()
            cur["date"] = v[:10] if (v and v[0].isdigit()) else None
    if cur:
        flush()
    return out


async def exa_search(query: str, num: int | None = None) -> tuple[str, list[dict]]:
    """Один поисковый запрос через Exa MCP. Весь обмен пишется в лог."""
    num = num or config.EXA_NUM_RESULTS
    env = {"EXA_API_KEY": config.EXA_API_KEY}
    raw_text = ""
    sources: list[dict] = []

    async with MCPStdioClient("npx", ["-y", "exa-mcp-server"], env=env, name="Exa") as client:
        await client.initialize()
        tools = await client.list_tools()

        # выбираем инструмент поиска (имя содержит "search")
        search_tool = next((t for t in tools if "search" in (t.get("name") or "").lower()), None)
        if not search_tool:
            await bus.log("🔌 [MCP:Exa] инструмент поиска не найден", "error")
            return "", []
        name = search_tool["name"]

        # подбираем имена аргументов из схемы инструмента
        props = ((search_tool.get("inputSchema") or {}).get("properties") or {})
        arguments: dict = {}
        qkey = "query" if "query" in props else next((k for k in props if "quer" in k.lower()), "query")
        arguments[qkey] = query
        for cand in ("numResults", "num_results", "num", "limit", "count"):
            if cand in props:
                arguments[cand] = num
                break

        res = await client.call_tool(name, arguments)

        # собираем текст из content[]
        parts = []
        for c in res.get("content", []):
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        raw_text = "\n".join(parts)
        sources = _parse_exa(raw_text)

        await bus.log(f"🔌 [MCP:Exa] ⬅️ result: {len(raw_text)} символов, статей: {len(sources)}")
        for s in sources[:8]:
            await bus.log(f"        • [{s.get('date') or 'дата?'}] {(s.get('title') or '')[:70]}  —  {s.get('url')}")

    return raw_text, sources
