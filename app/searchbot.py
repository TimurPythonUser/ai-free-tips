"""Публичный «Поисковик халявы» в личке бота.

Любой человек пишет боту запрос («нужна бесплатная генерация видео») — бот ищет
по базе опубликованных халяв (history.json) и отвечает подборкой со ссылками.

Гейт: перед ответом проверяем подписку на канал — не подписан → просим подписаться.
Так полезный бот превращается в машину подписчиков: польза в обмен на подписку.

Защита от разорения: лимит запросов на человека в день + короткие запросы.
Подключается к УЖЕ работающему Telethon bot-клиенту юзербота: searchbot.register(bot).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from telethon import Button, errors, events

from . import clients, config, store
from .logbus import bus

# Сколько запросов в день разрешаем одному человеку (DeepSeek стоит копейки,
# но открытый бот без лимита — приглашение выжечь ключ).
DAILY_PER_USER = 15
MAX_QUERY_LEN = 200

_SEARCH_SYSTEM = """Ты — поисковик по базе ИИ-халяв (бесплатные доступы, кредиты, гранты).
Тебе дают запрос человека и нумерованный список халяв из базы.
Выбери до 3 штук, которые РЕАЛЬНО отвечают на запрос. Смотри на суть, а не на слова:
«бесплатный фотошоп с ИИ» ≈ генерация/редактирование картинок.
Если ничего по сути не подходит — верни пустой список, не притягивай за уши.

Ответь СТРОГО JSON: {"matches": [номера], "tip": "одна короткая фраза-совет по запросу или пустая строка"}"""

_state_cache: dict = {}


# ---------- лимиты ----------

def _limits() -> dict:
    today = date.today().isoformat()
    data = {}
    if config.SEARCHBOT_LIMITS_FILE.exists():
        try:
            data = json.loads(config.SEARCHBOT_LIMITS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if data.get("date") != today:
        data = {"date": today, "users": {}}
    return data


def _limit_hit(user_id: int) -> bool:
    data = _limits()
    n = data["users"].get(str(user_id), 0)
    if n >= DAILY_PER_USER:
        return True
    data["users"][str(user_id)] = n + 1
    config.SEARCHBOT_LIMITS_FILE.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return False


# ---------- база и поиск ----------

def _catalog() -> list[dict]:
    """Опубликованные халявы, свежие в конце → разворачиваем свежими вперёд."""
    return list(reversed(store.load()))


def _fresh_mark(posted_at: str) -> str:
    try:
        days = (datetime.now() - datetime.fromisoformat(posted_at)).days
    except Exception:
        return ""
    if days > 45:
        return " ⚠️ давняя, могла закончиться"
    return ""


def _parse_matches(raw: str) -> tuple[list[int], str]:
    raw = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if m:
        raw = m.group(1).strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e > s:
        try:
            data = json.loads(raw[s:e + 1])
            nums = [int(x) for x in (data.get("matches") or [])][:3]
            return nums, str(data.get("tip") or "").strip()
        except Exception:
            pass
    return [], ""


async def _search(query: str) -> tuple[list[dict], str]:
    items = _catalog()[:80]  # свежайшие 80 — за глаза, и промпт не раздувается
    if not items:
        return [], ""
    lines = []
    for i, it in enumerate(items, 1):
        d = (it.get("posted_at") or "")[:10]
        lines.append(f"{i}. [{d}] {it.get('title', '')}")
    raw = await clients.deepseek(
        [{"role": "system", "content": _SEARCH_SYSTEM},
         {"role": "user", "content": f"Запрос: {query}\n\nБаза:\n" + "\n".join(lines)}],
        temperature=0.3,
    )
    nums, tip = _parse_matches(raw)
    found = [items[n - 1] for n in nums if 1 <= n <= len(items)]
    return found, tip


def _format_answer(found: list[dict], tip: str) -> str:
    ch = config.TELEGRAM_CHANNEL.lstrip("@")
    parts = ["🎁 Вот что нашлось:"]
    for it in found:
        title = it.get("title", "без названия")
        mark = _fresh_mark(it.get("posted_at") or "")
        line = f"\n• **{title}**{mark}"
        if it.get("url"):
            line += f"\n  ссылка: {it['url']}"
        if it.get("message_id"):
            line += f"\n  подробнее: https://t.me/{ch}/{it['message_id']}"
        parts.append(line)
    if tip:
        parts.append(f"\n💡 {tip}")
    parts.append(f"\nНовые халявы — каждый день в @{ch}")
    return "\n".join(parts)


# ---------- подключение к боту ----------

def register(bot) -> None:
    """Вешаем обработчики на существующий Telethon bot-клиент юзербота."""
    channel_holder = {}

    async def _is_subscribed(user_id: int) -> bool:
        try:
            if "ch" not in channel_holder:
                channel_holder["ch"] = await bot.get_entity(config.TELEGRAM_CHANNEL)
            await bot.get_permissions(channel_holder["ch"], user_id)
            return True
        except errors.UserNotParticipantError:
            return False
        except Exception:
            return True  # не смогли проверить — не мучаем человека, отвечаем

    def _join_kb():
        return [[Button.url("👉 Подписаться на канал",
                            f"https://t.me/{config.TELEGRAM_CHANNEL.lstrip('@')}")]]

    @bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_search(event):
        text = (event.raw_text or "").strip()
        sender_id = event.sender_id
        # служебные команды владельца обрабатываются в userbot.py — не трогаем
        if str(sender_id) == str(config.OWNER_CHAT_ID) and text.startswith(("/report", "/scout")):
            return
        if not text or event.via_bot_id:
            return

        if text.startswith("/start"):
            await event.reply(
                "Привет! Я — поисковик ИИ-халявы 🎁\n\n"
                "Напиши, что ищешь, например:\n"
                "• «бесплатная генерация видео»\n"
                "• «бесплатная альтернатива Midjourney»\n"
                "• «гранты для ИИ-стартапа»\n\n"
                "Я поищу по базе проверенных халяв и дам ссылки.")
            return
        if text.startswith("/"):
            # незнакомая команда — не гоним её в поиск, подсказываем формат
            await event.reply("Просто напиши, что ищешь, обычным текстом — "
                              "например «бесплатная генерация музыки» 🎁")
            return

        if len(text) > MAX_QUERY_LEN:
            await event.reply("Слишком длинный запрос — сформулируй короче, в пару слов 🙏")
            return

        if not await _is_subscribed(sender_id):
            await event.reply(
                "Поиск бесплатный для подписчиков канала 🎁\n"
                "Подпишись — и возвращайся, я найду тебе халяву:",
                buttons=_join_kb())
            return

        if _limit_hit(sender_id):
            await event.reply("На сегодня лимит запросов исчерпан — приходи завтра 🙌")
            return

        try:
            async with bot.action(event.chat_id, "typing"):
                found, tip = await _search(text)
        except Exception as e:
            await bus.log(f"🔍 поисковик: ошибка ({type(e).__name__})", "warn")
            await event.reply("Что-то пошло не так, попробуй ещё раз чуть позже 🙏")
            return

        if found:
            await event.reply(_format_answer(found, tip), link_preview=False)
            await bus.log(f"🔍 поисковик: ответил на «{text[:40]}» ({len(found)} шт.)")
        else:
            ch = config.TELEGRAM_CHANNEL.lstrip("@")
            await event.reply(
                "Пока по этому запросу в базе пусто 😕\n"
                f"Но канал пополняется каждый день — загляни: @{ch}\n"
                "И попробуй переформулировать — например «генерация картинок» вместо названия сервиса.")
            await bus.log(f"🔍 поисковик: пусто по «{text[:40]}»")
