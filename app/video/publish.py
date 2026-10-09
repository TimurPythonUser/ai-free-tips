"""Публикация ролика: sendVideo в канал (автомат) + в личку владельцу с готовыми
подписями под TikTok/Shorts (ручная выкладка, пока API-аудиты не пройдены)."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

import httpx

from .. import config
from ..logbus import bus

_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
_FOOTER = "📢 @ai_free_tips"


def _invite_links() -> dict:
    """Ссылки-счётчики ({"tiktok": url, "shorts": url}); создаёт их юзербот при старте."""
    try:
        if config.INVITE_LINKS_FILE.exists():
            return json.loads(config.INVITE_LINKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _threads_caption(script: dict, source_url: str | None = None) -> str:
    """Короткий Threads-пост по ролику: хук + фраза + ссылка на источник + счётчик.
    Компактно, чтобы влезало целиком. Видео владелец приложит сам."""
    hook = (script.get("hook") or "").strip().rstrip(".")
    phrases = [p.strip().rstrip(".") for p in (script.get("phrases") or []) if p.strip()]
    body = ". ".join([p for p in ([hook] + phrases[:1]) if p]) + "."
    tg = _invite_links().get("threads") or \
        f"https://t.me/{config.TELEGRAM_CHANNEL.lstrip('@')}"
    lines = [body]
    if source_url:
        lines.append(f"🔗 Источник: {source_url}")
    lines.append(f"📢 Все халявы: {tg}")
    return "\n\n".join(lines)


def _tiktok_caption(text: str) -> str:
    """TikTok режет охват за внешние ссылки в описании (особенно t.me) — убираем
    любые ссылки и упоминания канала, зовём в профиль: там ссылка легальна.
    Замечено на практике: просмотры упали вдвое, как только в подписях появился t.me."""
    if not text:
        return text
    ch = re.escape(config.TELEGRAM_CHANNEL.lstrip("@"))
    out = re.sub(rf"https?://t\.me/\S+|t\.me/\S+|@{ch}", "", text)
    # выпиливаем осиротевшие подводки к бывшей ссылке («Все ИИ-халявы:», «Ссылки:»)
    out = re.sub(r"(?im)^[^\n]*(ссылк|халяв)[^\n]*:\s*$", "", out)
    out = re.sub(r"(?i)(все|вся)[^\n#]*(ссылки|халявы)[^\n#]*:\s*", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{2,}", "\n", out).strip()
    if "профил" not in out.lower():
        out += "\n🎁 Всё бесплатное — ссылка в профиле"
    return out


def _swap_link(text: str, link: str) -> str:
    """Меняем в подписи обычную ссылку канала на счётчик; если ссылки нет — дописываем.
    Так в статистике видно, с какой площадки пришёл подписчик."""
    if not text or not link:
        return text
    ch = re.escape(config.TELEGRAM_CHANNEL.lstrip("@"))
    out = re.sub(rf"https?://t\.me/{ch}|t\.me/{ch}|@{ch}", link, text)
    if link not in out:
        out += f"\n🎁 Все ИИ-халявы: {link}"
    return out


async def _send_video(c: httpx.AsyncClient, chat_id: str, video: Path,
                      caption: str) -> tuple[bool, str]:
    with open(video, "rb") as f:
        r = await c.post(f"{_API}/sendVideo",
                         data={"chat_id": chat_id, "caption": caption[:1024],
                               "supports_streaming": "true"},
                         files={"video": (video.name, f, "video/mp4")})
    data = r.json()
    if r.status_code == 200 and data.get("ok"):
        return True, "ok"
    return False, str(data.get("description") or r.text[:200])


async def to_channel(video: Path, script: dict, item: dict) -> bool:
    """Ролик в канал: хук + ссылка + футер."""
    parts = [script.get("hook", "").strip()]
    url = (item.get("url") or "").strip()
    if url:
        parts.append(f"⚡ Забирай тут: {url}")
    parts.append(_FOOTER)
    caption = "\n\n".join(p for p in parts if p)

    async with httpx.AsyncClient(timeout=180) as c:
        ok, msg = await _send_video(c, config.TELEGRAM_CHANNEL, video, caption)
    if ok:
        await bus.log("      ✅ ролик опубликован в канале", "success")
    else:
        await bus.log(f"      ❌ канал не принял видео: {msg}", "error")
    return ok


async def to_owner(video: Path, script: dict, source_url: str | None = None) -> bool:
    """Ролик + подписи в личку: скопировал текст — выложил в TikTok/Shorts за минуту."""
    if not config.OWNER_CHAT_ID:
        await bus.log("      ℹ OWNER_CHAT_ID не задан — ролик в личку не шлю. "
                      "Узнай свой id у @userinfobot и впиши в .env", "warn")
        return False

    captions = script.get("captions") or {}
    links = _invite_links()
    # Только YouTube (TikTok на паузе, Threads в бане — их подписи убраны).
    shorts = (captions.get("shorts") or "").strip()  # заголовок — без ссылок
    shorts_descr = _swap_link((captions.get("shorts_description") or "").strip(),
                              links.get("shorts"))
    text_parts = ["🎬 <b>Ролик готов — выложи на YouTube:</b>"]
    if shorts:
        text_parts.append("▶️ <b>«Название»</b> "
                          "(грузишь файл как есть — подставится само):\n"
                          f"<pre>{html.escape(shorts)}</pre>")
    if shorts_descr:
        text_parts.append("▶️ <b>«Описание»:</b>\n"
                          f"<pre>{html.escape(shorts_descr)}</pre>")
    text_parts.append("👆 Жми на текст в рамке — он скопируется целиком")

    async with httpx.AsyncClient(timeout=180) as c:
        ok, msg = await _send_video(c, config.OWNER_CHAT_ID, video, "🎬 Ролик готов")
        if not ok:
            await bus.log(f"      ❌ не смог прислать ролик в личку: {msg}. "
                          "Проверь, что ты писал боту /start", "error")
            return False
        r = await c.post(f"{_API}/sendMessage", json={
            "chat_id": config.OWNER_CHAT_ID,
            "text": "\n\n".join(text_parts),
            "parse_mode": "HTML",
        })
        if not (r.status_code == 200 and r.json().get("ok")):
            await bus.log("      ⚠ подписи в личку не ушли (ролик ушёл)", "warn")
    await bus.log("      ✅ ролик и подписи отправлены тебе в личку", "success")
    return True
