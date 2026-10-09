"""Служебные уведомления владельцу в личку (сбои, пустые слоты).
Тихо молчит, если OWNER_CHAT_ID не задан или отправка не удалась — уведомление
не должно ронять основной процесс."""
from __future__ import annotations

import httpx

from . import config
from .logbus import bus


async def owner(text: str) -> None:
    if not config.OWNER_CHAT_ID or not config.TELEGRAM_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            await c.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": config.OWNER_CHAT_ID, "text": text},
            )
    except Exception as e:
        await bus.log(f"      ⚠ не смог отправить уведомление владельцу: {type(e).__name__}", "warn")


async def owner_photo(photo_url: str, html_caption: str) -> None:
    """Фото + HTML-подпись владельцу (напр. картинка для Threads-поста).
    При неудаче тихо откатывается на обычный owner_html."""
    if not config.OWNER_CHAT_ID or not config.TELEGRAM_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendPhoto",
                json={"chat_id": config.OWNER_CHAT_ID, "photo": photo_url,
                      "caption": html_caption[:1024], "parse_mode": "HTML"},
            )
            if r.status_code == 200 and r.json().get("ok"):
                return
    except Exception:
        pass
    await owner_html(html_caption)  # картинка не взлетела — хотя бы текст


async def owner_html(text: str) -> None:
    """То же, но с HTML-разметкой (для <pre>-блоков «жми — скопируется»)."""
    if not config.OWNER_CHAT_ID or not config.TELEGRAM_BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            await c.post(
                f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": config.OWNER_CHAT_ID, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
            )
    except Exception as e:
        await bus.log(f"      ⚠ не смог отправить HTML-уведомление владельцу: {type(e).__name__}", "warn")
