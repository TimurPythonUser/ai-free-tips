"""HTTP-клиенты к DeepSeek и Perplexity (оба — OpenAI-совместимый chat API).
Все сетевые вызовы обёрнуты в ретраи с экспоненциальным backoff."""
from __future__ import annotations

import asyncio

import httpx

from . import config
from .logbus import bus


async def _with_retries(coro_factory, what: str):
    """Выполняет сетевой вызов с ретраями. coro_factory — функция без аргументов,
    возвращающая свежую корутину на каждую попытку (httpx-запрос нельзя переиспользовать)."""
    attempts = max(1, config.HTTP_RETRIES)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            last_exc = e
            # 4xx (кроме 429) повторять бессмысленно — это не временная ошибка
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                raise
            if attempt < attempts:
                delay = config.HTTP_RETRY_BACKOFF ** (attempt - 1)
                await bus.log(
                    f"      ⏳ [{what}] попытка {attempt}/{attempts} не удалась "
                    f"({type(e).__name__}) — повтор через {delay:.0f}с", "warn"
                )
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


async def _chat(base_url: str, api_key: str, model: str, messages: list,
                temperature: float = 0.2, timeout: float = 120.0) -> dict:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages, "temperature": temperature}

    async def _do():
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
            r.raise_for_status()
            return r.json()

    return await _with_retries(_do, f"HTTP {base_url}")


async def deepseek(messages: list, temperature: float = 0.2, model: str | None = None) -> str:
    """Возвращает текст ответа DeepSeek. model=None -> DEEPSEEK_MODEL."""
    data = await _chat(
        config.DEEPSEEK_BASE_URL, config.DEEPSEEK_API_KEY, model or config.DEEPSEEK_MODEL,
        messages, temperature,
    )
    return data["choices"][0]["message"]["content"]


async def perplexity(query: str, system: str | None = None) -> tuple[str, list, list]:
    """Возвращает (текст ответа, citations, search_results)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": query})

    data = await _chat(
        config.PERPLEXITY_BASE_URL, config.PERPLEXITY_API_KEY, config.PERPLEXITY_MODEL,
        messages, temperature=0.2,
    )
    content = data["choices"][0]["message"]["content"]
    citations = data.get("citations") or []
    search_results = data.get("search_results") or []
    return content, citations, search_results
