"""Движки поиска через ПРЯМОЙ HTTP API (без MCP-stdio — так быстрее и надёжнее):
  • Tavily  — https://api.tavily.com/search  (Bearer-токен)
  • Exa     — https://api.exa.ai/search      (заголовок x-api-key)

Обе функции возвращают унифицированное (raw_text, sources), где
sources = [{"url": str, "title": str|None, "date": "YYYY-MM-DD"|None}].
Все сетевые вызовы идут через общий ретрай-хелпер clients._with_retries."""
from __future__ import annotations

from email.utils import parsedate_to_datetime

import httpx

from . import clients, config
from .logbus import bus


def _norm_date(v) -> str | None:
    """Приводим дату из ответа движка к 'YYYY-MM-DD' или None (не выдумываем).
    Понимаем ISO ('2025-06-01T…', '2025/06/01') и RFC 2822
    ('Sat, 04 Jul 2026 09:00:00 GMT' — так даты отдаёт Tavily topic=news)."""
    if not v:
        return None
    s = str(v).strip()
    # ISO-подобное
    head = s[:10].replace("/", "-")
    parts = head.split("-")
    if len(parts) == 3 and parts[0].isdigit() and len(parts[0]) == 4:
        return head
    # RFC 2822
    try:
        dt = parsedate_to_datetime(s)
        if dt:
            return dt.strftime("%Y-%m-%d")
    except Exception:
        pass
    return None


def _digest(sources: list[dict]) -> str:
    """Читабельный текст-дайджест для передачи редактору (DeepSeek)."""
    lines = []
    for s in sources:
        date = s.get("date") or "дата?"
        title = s.get("title") or ""
        snippet = (s.get("_snippet") or "").strip().replace("\n", " ")
        line = f"- [{date}] {title} — {s.get('url')}"
        if snippet:
            line += f"\n  {snippet[:280]}"
        lines.append(line)
    return "\n".join(lines) or "(пусто)"


async def tavily_search(query: str) -> tuple[str, list[dict]]:
    """Поиск через Tavily. topic=news + time_range дают свежесть и даты публикации."""
    if not config.TAVILY_API_KEY:
        return "", []
    headers = {
        "Authorization": f"Bearer {config.TAVILY_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query[:400],  # у Tavily лимит query — 400 символов
        "search_depth": config.TAVILY_SEARCH_DEPTH,
        "topic": config.TAVILY_TOPIC,
        "max_results": config.TAVILY_MAX_RESULTS,
        "include_answer": True,
    }
    if config.TAVILY_TIME_RANGE:
        payload["time_range"] = config.TAVILY_TIME_RANGE

    await bus.log(f"🔎 [Tavily] ➡️ POST {config.TAVILY_BASE_URL}/search "
                  f"(topic={config.TAVILY_TOPIC}, time_range={config.TAVILY_TIME_RANGE}, "
                  f"max={config.TAVILY_MAX_RESULTS})")

    async def _do():
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{config.TAVILY_BASE_URL}/search", headers=headers, json=payload)
            r.raise_for_status()
            return r.json()

    data = await clients._with_retries(_do, "Tavily")
    sources: list[dict] = []
    for res in data.get("results", []) or []:
        if not isinstance(res, dict) or not res.get("url"):
            continue
        sources.append({
            "url": res.get("url"),
            "title": res.get("title"),
            "date": _norm_date(res.get("published_date") or res.get("publishedDate")),
            "_snippet": res.get("content") or "",
        })
    answer = (data.get("answer") or "").strip()
    raw_text = (f"Сводка Tavily: {answer}\n\n" if answer else "") + _digest(sources)
    dated = sum(1 for s in sources if s["date"])
    await bus.log(f"🔎 [Tavily] ⬅️ {len(sources)} результатов (с датами: {dated})")
    return raw_text, sources


async def exa_search(query: str, num: int | None = None) -> tuple[str, list[dict]]:
    """Поиск через Exa HTTP API. type=auto, короткий контент для дайджеста."""
    if not config.EXA_API_KEY:
        return "", []
    num = num or config.EXA_NUM_RESULTS
    headers = {
        "x-api-key": config.EXA_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "numResults": num,
        "type": "auto",
        "contents": {"text": {"maxCharacters": 400}},
    }

    await bus.log(f"🔎 [Exa] ➡️ POST {config.EXA_BASE_URL}/search (numResults={num})")

    async def _do():
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{config.EXA_BASE_URL}/search", headers=headers, json=payload)
            r.raise_for_status()
            return r.json()

    data = await clients._with_retries(_do, "Exa")
    sources: list[dict] = []
    for res in data.get("results", []) or []:
        if not isinstance(res, dict) or not (res.get("url") or res.get("id")):
            continue
        sources.append({
            "url": res.get("url") or res.get("id"),
            "title": res.get("title"),
            "date": _norm_date(res.get("publishedDate") or res.get("published_date")),
            "_snippet": res.get("text") or "",
        })
    raw_text = _digest(sources)
    dated = sum(1 for s in sources if s["date"])
    await bus.log(f"🔎 [Exa] ⬅️ {len(sources)} результатов (с датами: {dated})")
    return raw_text, sources
