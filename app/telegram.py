"""Публикация постов в Telegram-канал через Bot API.
Тело поста приходит готовым от креативщика (item['text_body']); здесь мы лишь
гарантируем ссылку и подпись канала, следим за лимитом и страхуемся от кривых HTML-тегов."""
from __future__ import annotations

import html
import re

import httpx

from . import config

_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
_FOOTER = "📢 @ai_free_tips  |  🔍 Найти халяву: @AI_freetips_bot"

_STEP_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣"]


def _legacy_body(item: dict) -> str:
    """Откат к старому формату (title/hook/benefit/steps/description), если креативщик
    не дал text_body. Возвращает тело БЕЗ ссылки и футера."""
    esc = lambda s: html.escape((s or "").strip())
    emoji = item.get("emoji") or "🔥"
    title = esc(item.get("title", ""))
    hook = esc(item.get("hook", ""))
    benefit = esc(item.get("benefit") or item.get("what", ""))
    steps = [esc(s) for s in (item.get("steps") or []) if (s or "").strip()]
    desc = esc(item.get("description", ""))

    head_emoji = "" if (title and ord(title[0]) > 0x2100) else f"{emoji} "
    parts = [f"{head_emoji}<b>{title}</b>"]
    if hook:
        parts.append(hook)
    if benefit:
        parts.append(f"🎁 <b>Что дают:</b> {benefit}")
    if steps:
        lines = ["📲 <b>Как забрать:</b>"]
        for i, s in enumerate(steps):
            lines.append(f"{_STEP_EMOJI[i] if i < len(_STEP_EMOJI) else '▪️'} {s}")
        parts.append("\n".join(lines))
    elif desc:
        parts.append(desc)
    return "\n\n".join(parts)


def _caption(item: dict, limit: int) -> str:
    """Готовый пост = тело (от креативщика) + ссылка + футер, в рамках лимита.
    Ссылка обязательна; при переполнении режем ТЕЛО, ссылку и футер сохраняем."""
    body = (item.get("text_body") or "").strip() or _legacy_body(item)
    url = (item.get("url", "") or "").strip()

    tail_parts = []
    if url:
        # честная подпись: прямая ссылка — «забирай», новость — «источник/подробности»
        if item.get("link_type") == "news":
            tail_parts.append(f"📰 Подробности и источник: {url}")
        else:
            tail_parts.append(f"⚡ Забирай тут: {url}")
    tail_parts.append(_FOOTER)
    tail = "\n\n".join(tail_parts)

    full = f"{body}\n\n{tail}"
    if len(full) <= limit:
        return full

    # не влезаем — подрезаем тело, сохраняя ссылку+футер
    budget = limit - len(tail) - 2  # 2 на разделитель
    if budget < 0:
        return tail[:limit]
    trimmed = body[:budget].rstrip()
    if len(trimmed) < len(body):
        trimmed = trimmed[: max(0, len(trimmed) - 1)].rstrip() + "…"
    return f"{trimmed}\n\n{tail}"


def _strip_tags(text: str) -> str:
    """Снимаем HTML-теги для аварийной отправки без parse_mode."""
    return re.sub(r"<[^>]+>", "", text or "")


async def _send(c: httpx.AsyncClient, method: str, payload: dict) -> tuple[bool, str, int | None]:
    """Один вызов Bot API. При ошибке разбора HTML-сущностей — повтор без parse_mode."""
    try:
        r = await c.post(f"{_API}/{method}", json=payload)
        data = r.json()
        if r.status_code == 200 and data.get("ok"):
            return True, "ok", data["result"].get("message_id")
        desc = str(data.get("description", ""))
        # кривые теги от модели → шлём тем же методом, но без разметки
        if "parse entities" in desc.lower() or "can't parse" in desc.lower():
            plain = dict(payload)
            plain.pop("parse_mode", None)
            key = "caption" if "caption" in plain else "text"
            plain[key] = _strip_tags(plain.get(key, ""))
            r2 = await c.post(f"{_API}/{method}", json=plain)
            d2 = r2.json()
            if r2.status_code == 200 and d2.get("ok"):
                return True, "ok (без разметки)", d2["result"].get("message_id")
            return False, f"HTTP {r2.status_code}: {d2.get('description', r2.text[:200])}", None
        return False, f"HTTP {r.status_code}: {desc or r.text[:200]}", None
    except Exception as e:
        return False, str(e), None


async def post(item: dict) -> tuple[bool, str, int | None]:
    """С картинкой -> sendPhoto, иначе (или при провале фото) -> sendMessage.
    Возвращает (успех, режим, message_id)."""
    img = item.get("image")
    async with httpx.AsyncClient(timeout=60) as c:
        if img:
            ok, mode, mid = await _send(c, "sendPhoto", {
                "chat_id": config.TELEGRAM_CHANNEL,
                "photo": img,
                "caption": _caption(item, 1024),
                "parse_mode": "HTML",
            })
            if ok:
                return True, f"фото+текст ({mode})", mid
            # фото не ушло — падаем в текстовый режим

        ok, mode, mid = await _send(c, "sendMessage", {
            "chat_id": config.TELEGRAM_CHANNEL,
            "text": _caption(item, 4096),
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })
        if ok:
            return True, f"текст ({mode})", mid
        return False, mode, None
