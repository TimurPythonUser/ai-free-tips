"""Комментинг-юзербот (АВТОМАТ С ПОДТВЕРЖДЕНИЕМ).

Как работает:
  • user-клиент (твой аккаунт, Telethon) слушает новые посты в каналах-донорах;
  • на свежий пост — генерит 2 живых черновика (DeepSeek) и через БОТА шлёт тебе
    в личку сообщение с кнопками [Вариант 1][Вариант 2][Пропустить];
  • жмёшь кнопку — user-клиент постит выбранный коммент ОТ ИМЕНИ КАНАЛА
    (send_as) прямо в комментарии под тем постом, с человекоподобной задержкой;
  • лимиты (в сутки / на канал / паузы) — чтоб не спалиться.

ВАЖНО: постит ТОЛЬКО от имени канала. Если send_as недоступен в конкретной
группе — коммент пропускается (никогда не палим личный аккаунт).

Запуск (после логина, см. login_userbot.py):  python -m app.userbot
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import date

from telethon import Button, TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (GetChannelRecommendationsRequest,
                                            GetFullChannelRequest, JoinChannelRequest)
from telethon.tl.functions.messages import (ExportChatInviteRequest,
                                            GetExportedChatInvitesRequest)

from . import commenting, config, searchbot
from .logbus import bus

# pending[token] = {"channel": str, "post_id": int, "variants": [str, str], "dm_msg": Message}
_pending: dict[str, dict] = {}
_seq = 0  # счётчик токенов (Math.random недоступен — используем инкремент)
_last_offer_ts = time.time()  # когда последний раз предлагали коммент (для фолбэка по простою)


# ---------- память: какие посты уже предлагали (с автоочисткой) ----------

def _seen_load() -> list[str]:
    if config.USERBOT_SEEN_FILE.exists():
        try:
            data = json.loads(config.USERBOT_SEEN_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def _seen_has(key: str) -> bool:
    return key in set(_seen_load())


def _commentable(m) -> bool:
    """У поста реально открыты комментарии? (флаг из самого сообщения, без доп. запросов)
    Отсекает посты с закрытыми комментами — их постить нельзя (MsgIdInvalidError)."""
    replies = getattr(m, "replies", None)
    return bool(replies) and bool(getattr(replies, "comments", False))


def _seen_add(key: str) -> None:
    """Добавляем ключ и держим не больше USERBOT_SEEN_MAX (старое выкидываем)."""
    items = _seen_load()
    if key in items:
        return
    items.append(key)
    if len(items) > config.USERBOT_SEEN_MAX:
        items = items[-config.USERBOT_SEEN_MAX:]
    config.USERBOT_SEEN_FILE.write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )


# ---------- журнал событий (для суточного отчёта и ротации каналов) ----------

def _stats_load() -> list[dict]:
    if config.USERBOT_STATS_FILE.exists():
        try:
            data = json.loads(config.USERBOT_STATS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def _stat(ev: str, ch: str, post_id: int, **extra) -> None:
    """Событие в журнал: offer/skip/redo/approve/limit/posted/failed.
    Храним 30 дней. Никогда не роняет бота — аналитика не важнее работы."""
    try:
        items = _stats_load()
        items.append({"ts": time.time(), "ev": ev, "ch": ch, "post": post_id, **extra})
        cutoff = time.time() - 30 * 86400
        items = [x for x in items if x.get("ts", 0) >= cutoff]
        config.USERBOT_STATS_FILE.write_text(
            json.dumps(items, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _stats_agg(evts: list[dict]) -> dict:
    """Сводка по каналам: {ch: {offer, posted, failed, skip, limit, errs{}}}."""
    per: dict[str, dict] = {}
    for x in evts:
        d = per.setdefault(x["ch"], {"offer": 0, "posted": 0, "failed": 0,
                                     "skip": 0, "limit": 0, "errs": {}})
        ev = x.get("ev")
        if ev in ("offer", "posted", "failed", "skip", "limit"):
            d[ev] += 1
        if ev == "failed":
            err = x.get("err", "?")
            d["errs"][err] = d["errs"].get(err, 0) + 1
    return per


def _report_text(day: list[dict], week: list[dict], deleted: dict[str, int]) -> str:
    """Текст суточного отчёта. deleted = {канал: сколько наших комментов исчезло}."""
    per = _stats_agg(day)
    lines = ["📊 Отчёт по комментингу за сутки", ""]
    if not per:
        lines.append("Событий не было — черновики не предлагались (проверь вечерний разбор).")
    else:
        total_posted = sum(d["posted"] for d in per.values())
        total_offer = sum(d["offer"] for d in per.values())
        lines.append(f"Итого: предложено {total_offer}, запощено {total_posted}, "
                     f"удалено после постинга {sum(deleted.values())}")
        lines.append("")
        for ch in sorted(per, key=lambda c: (-per[c]["posted"], -per[c]["offer"])):
            d = per[ch]
            bits = []
            if d["offer"]:
                bits.append(f"📨{d['offer']}")
            if d["posted"]:
                bits.append(f"✅{d['posted']}")
            if d["failed"]:
                errs = ",".join(f"{k}×{v}" for k, v in d["errs"].items())
                bits.append(f"❌{d['failed']}({errs})")
            if d["skip"]:
                bits.append(f"⏭{d['skip']}")
            if d["limit"]:
                bits.append(f"⏸{d['limit']}")
            if deleted.get(ch):
                bits.append(f"🧹удалён:{deleted[ch]} (похоже, антиспам)")
            lines.append(f"@{ch}: " + "  ".join(bits))
        lines.append("")
        lines.append("📨 предложено | ✅ запощено | ❌ ошибка | ⏭ пропустил ты | "
                     "⏸ лимит | 🧹 удалили после постинга")

    # недельная сводка — кто кандидат на вылет
    per_w = _stats_agg(week)
    bad = []
    for ch, d in sorted(per_w.items()):
        if d["posted"] == 0 and (d["failed"] > 0 or d["offer"] >= 3):
            bad.append(f"🔻 @{ch}: за 7 дней 0 запощено "
                       f"(предложено {d['offer']}, ошибок {d['failed']})")
    if bad:
        lines += ["", "Кандидаты на замену (по итогам недели):"] + bad + \
                 ["", "Разведка новых каналов: отправь боту команду /scout"]
    return "\n".join(lines)


# ---------- суточные лимиты ----------

def _limits_load() -> dict:
    today = date.today().isoformat()
    if config.USERBOT_STATE_FILE.exists():
        try:
            data = json.loads(config.USERBOT_STATE_FILE.read_text(encoding="utf-8"))
            if data.get("date") == today:
                return data
        except Exception:
            pass
    return {"date": today, "total": 0, "per_channel": {}}


def _limits_save(data: dict) -> None:
    config.USERBOT_STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _allowed(channel: str) -> tuple[bool, str]:
    st = _limits_load()
    if st["total"] >= config.USERBOT_DAILY_MAX:
        return False, f"дневной лимит {config.USERBOT_DAILY_MAX} исчерпан"
    if st["per_channel"].get(channel, 0) >= config.USERBOT_PER_CHANNEL_DAILY:
        return False, f"по @{channel} лимит на сегодня исчерпан"
    return True, ""


def _count(channel: str) -> None:
    st = _limits_load()
    st["total"] += 1
    st["per_channel"][channel] = st["per_channel"].get(channel, 0) + 1
    _limits_save(st)


# ---------- клиенты ----------

def _make_clients() -> tuple[TelegramClient, TelegramClient]:
    user = TelegramClient(StringSession(config.USERBOT_SESSION),
                          config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    # bot-сессия — файлом в data/ (переживает рестарт контейнера на смонтированном volume)
    bot = TelegramClient(str(config.DATA_DIR / "botsession"),
                         config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    return user, bot


async def run() -> None:
    if not (config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH and config.USERBOT_SESSION):
        await bus.log("❌ Юзербот: нет TELEGRAM_API_ID / API_HASH / USERBOT_SESSION в .env", "error")
        return
    if not config.OWNER_CHAT_ID:
        await bus.log("❌ Юзербот: не задан OWNER_CHAT_ID — некуда слать подтверждения", "error")
        return

    user, bot = _make_clients()
    await user.connect()
    if not await user.is_user_authorized():
        await bus.log("❌ Юзербот: сессия невалидна — перелогинься через login_userbot.py", "error")
        return
    await bot.start(bot_token=config.TELEGRAM_BOT_TOKEN)

    me = await user.get_me()
    channel_entity = await user.get_entity(config.TELEGRAM_CHANNEL)
    # каналы-доноры резолвим только если комментинг включён
    donors = commenting.channels() if config.USERBOT_COMMENTING else []
    donor_entities = []
    for ch in donors:
        try:
            donor_entities.append(await user.get_entity(ch))
        except Exception as e:
            await bus.log(f"   ⚠ @{ch}: не резолвится ({type(e).__name__}) — пропускаю", "warn")

    if config.USERBOT_COMMENTING:
        await bus.log(f"🤖 Юзербот запущен как @{me.username or me.id}. "
                      f"Слежу за {len(donor_entities)} каналами. Постить буду от {config.TELEGRAM_CHANNEL}.")
    else:
        await bus.log(f"🤖 Юзербот запущен как @{me.username or me.id}. "
                      "КОММЕНТИНГ ВЫКЛ — работают только бот-поисковик и команды /report, /scout.")

    # --- ссылки-счётчики: создаём один раз (дальше конвейер подставляет их в подписи,
    # а вечерний отчёт читает по ним статистику «откуда пришли подписчики») ---
    try:
        links: dict = {}
        if config.INVITE_LINKS_FILE.exists():
            links = json.loads(config.INVITE_LINKS_FILE.read_text(encoding="utf-8"))
        created = []
        for title, key in (("TikTok", "tiktok"), ("YouTube Shorts", "shorts"),
                           ("Bluesky", "bluesky")):
            if not links.get(key):
                inv = await user(ExportChatInviteRequest(peer=channel_entity, title=title))
                links[key] = inv.link
                created.append(f"• {title}: {inv.link}")
        if created:
            config.INVITE_LINKS_FILE.write_text(
                json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")
            await bot.send_message(int(config.OWNER_CHAT_ID),
                "🔗 Создал ссылки-счётчики подписчиков:\n\n" + "\n".join(created) +
                "\n\n📌 Что сделать один раз: поставь TikTok-ссылку в шапку профиля "
                "TikTok (вместо обычной). Shorts-ссылка сама пойдёт в описания роликов. "
                "Кто по какой пришёл — увидишь в вечернем отчёте.")
            await bus.log("🔗 Ссылки-счётчики созданы и отправлены владельцу", "success")
    except Exception as e:
        await bus.log(f"⚠ не смог создать ссылки-счётчики: {type(e).__name__}", "warn")

    def _render(token: str, ch: str, post_id: int, text: str, variants: list[str]):
        """Текст DM + клавиатура (общий рендер для показа и для «переделать»)."""
        excerpt = text[:250] + ("…" if len(text) > 250 else "")
        body = (f"💬 Новый пост в @{ch}\n\n{excerpt}\n\n"
                f"🔗 https://t.me/{ch}/{post_id}\n\n"
                f"1️⃣ {variants[0]}\n\n2️⃣ {variants[1]}")
        kb = [[Button.inline("1️⃣ Запостить", f"p|{token}|0".encode()),
               Button.inline("2️⃣ Запостить", f"p|{token}|1".encode())],
              [Button.inline("🔄 Переделать", f"redo|{token}".encode()),
               Button.inline("✖️ Пропустить", f"skip|{token}".encode())]]
        return body, kb

    async def _offer(ch: str, post_id: int, text: str) -> None:
        """Сгенерить черновики и прислать владельцу в личку с кнопками."""
        global _seq, _last_offer_ts
        if len(text.strip()) < 30:
            return  # короткие/сервисные посты пропускаем
        key = f"{ch}/{post_id}"
        if _seen_has(key):
            return  # этот пост уже предлагали — не дублируем
        # лимит постинга проверяем при НАЖАТИИ кнопки, а не при показе:
        # черновики показываем все, а публикуется — сколько разрешено
        variants = await commenting._draft(ch, text)
        if len(variants) < 1:
            return
        while len(variants) < 2:
            variants.append(variants[0])

        _seq += 1
        token = str(_seq)
        body, kb = _render(token, ch, post_id, text, variants)
        dm = await bot.send_message(int(config.OWNER_CHAT_ID), body, buttons=kb)
        _pending[token] = {"channel": ch, "post_id": post_id, "text": text,
                           "variants": variants, "dm_msg": dm, "render": _render}
        _seen_add(key)
        _stat("offer", ch, post_id)
        _last_offer_ts = time.time()
        await bus.log(f"   ✨ @{ch}: пост #{post_id} — жду твой выбор в личке")

    donor_names = [getattr(e, "username", None) for e in donor_entities]
    donor_names = [n for n in donor_names if n]

    # --- вечерний разбор: пачка черновиков со всех каналов ---
    async def _digest():
        await bus.log(f"🌙 Вечерний разбор: собираю до {config.COMMENT_DIGEST_MAX} черновиков…")
        offered = 0
        for ch in donor_names:
            if offered >= config.COMMENT_DIGEST_MAX:
                break
            try:
                async for m in user.iter_messages(ch, limit=10):
                    text = m.message or ""
                    if len(text.strip()) < 30 or _seen_has(f"{ch}/{m.id}"):
                        continue
                    if not _commentable(m):
                        continue  # комменты под постом закрыты — не предлагаем
                    before = len(_seen_load())
                    await _offer(ch, m.id, text)
                    if len(_seen_load()) > before:  # реально предложили
                        offered += 1
                    break
            except Exception as e:
                await bus.log(f"   ⚠ разбор @{ch}: {type(e).__name__}", "warn")
        await bus.log(f"🌙 Вечерний разбор готов: {offered} черновиков в личке", "success")

    # --- новый пост в канале-доноре (реалтайм, если включён) ---
    @user.on(events.NewMessage(chats=donor_entities))
    async def on_post(event):
        if not config.USERBOT_REALTIME or not _commentable(event.message):
            return
        ch = getattr(event.chat, "username", None) or str(event.chat_id)
        await _offer(ch, event.message.id, event.message.message or "")

    # --- суточный отчёт по комментингу (раз в день в COMMENT_REPORT_TIME) ---
    async def _report():
        await bus.log("📊 Собираю суточный отчёт по комментингу…")
        now = time.time()
        evts = _stats_load()
        day = [x for x in evts if now - x["ts"] <= 86400]
        week = [x for x in evts if now - x["ts"] <= 7 * 86400]

        # живы ли комменты, запощенные за сутки? (антиспам удаляет молча)
        deleted: dict[str, int] = {}
        for x in day:
            if x.get("ev") == "posted" and x.get("chat_id") and x.get("msg_id"):
                try:
                    msg = await user.get_messages(int(x["chat_id"]), ids=int(x["msg_id"]))
                    if msg is None:
                        deleted[x["ch"]] = deleted.get(x["ch"], 0) + 1
                    await asyncio.sleep(1)
                except Exception:
                    pass

        text = _report_text(day, week, deleted)

        # --- откуда пришли подписчики (ссылки-счётчики) + рост канала ---
        try:
            full = await user(GetFullChannelRequest(channel_entity))
            members = getattr(full.full_chat, "participants_count", 0) or 0
            res = await user(GetExportedChatInvitesRequest(
                peer=channel_entity, admin_id=me, limit=50))
            usage = {}
            for inv in res.invites:
                title = getattr(inv, "title", None)
                if title:  # считаем только именные (наши счётчики)
                    usage[title] = getattr(inv, "usage", 0) or 0

            snap = {}
            if config.INVITE_STATS_FILE.exists():
                try:
                    snap = json.loads(config.INVITE_STATS_FILE.read_text(encoding="utf-8"))
                except Exception:
                    snap = {}
            prev_links = snap.get("links", {})
            prev_members = snap.get("members")

            lines = ["", "— — —", f"👥 Подписчиков в канале: {members}"
                     + (f" ({members - prev_members:+d} за сутки)"
                        if isinstance(prev_members, int) else "")]
            tracked = 0
            for title, cnt in sorted(usage.items()):
                prev = prev_links.get(title)
                if isinstance(prev, int):
                    tracked += cnt - prev
                    lines.append(f"🔗 {title}: всего {cnt} ({cnt - prev:+d} за сутки)")
                else:
                    lines.append(f"🔗 {title}: всего {cnt}")
            if isinstance(prev_members, int):
                organic = (members - prev_members) - tracked
                lines.append(f"🌱 Прочее (профиль, комменты, поиск): {organic:+d}")

            config.INVITE_STATS_FILE.write_text(json.dumps(
                {"date": date.today().isoformat(), "links": usage, "members": members},
                ensure_ascii=False), encoding="utf-8")
            text += "\n".join(lines)
        except Exception as e:
            text += f"\n\n🔗 Статистика ссылок недоступна ({type(e).__name__})"

        for i in range(0, len(text), 3800):  # лимит телеги 4096 — режем с запасом
            await bot.send_message(int(config.OWNER_CHAT_ID), text[i:i + 3800],
                                   link_preview=False)
        await bus.log("📊 Отчёт отправлен в личку", "success")

    async def _report_scheduler():
        if not config.COMMENT_REPORT_TIME:
            return
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(config.TIMEZONE)
        except Exception:
            tz = None
        from datetime import datetime
        last_day = None
        while True:
            await asyncio.sleep(30)
            now_dt = datetime.now(tz)
            if (now_dt.strftime("%H:%M") == config.COMMENT_REPORT_TIME
                    and last_day != now_dt.date()):
                last_day = now_dt.date()
                await _report()

    # --- /scout в личке бота: разведка новых каналов через рекомендации Telegram ---
    @bot.on(events.NewMessage(pattern=r"^/(scout|report)"))
    async def on_command(event):
        if event.sender_id != int(config.OWNER_CHAT_ID):
            return
        if event.raw_text.startswith("/report"):
            await _report()
            return
        await event.reply("🔭 Разведка: спрашиваю у Telegram похожие каналы на наших доноров… "
                          "(~минуту-две)")
        donors_set = {n.lower() for n in donor_names}
        found: dict[str, int] = {}
        for ent in donor_entities:
            try:
                res = await user(GetChannelRecommendationsRequest(channel=ent))
                for chat in res.chats:
                    u = getattr(chat, "username", None)
                    if u and u.lower() not in donors_set and u not in found:
                        found[u] = getattr(chat, "participants_count", 0) or 0
                await asyncio.sleep(2)
            except Exception as e:
                await bus.log(f"   ⚠ рекомендации @{getattr(ent, 'username', '?')}: "
                              f"{type(e).__name__}", "warn")
        # топ-25 по подписчикам → комменты открыты? антиспам-бота нет?
        good = []
        for u, subs in sorted(found.items(), key=lambda kv: kv[1], reverse=True)[:25]:
            try:
                open_c = False
                async for m in user.iter_messages(u, limit=5):
                    if _commentable(m):
                        open_c = True
                        break
                if not open_c:
                    continue
                guard = await _antispam_guard(user, u)
                if guard:  # бот-модератор в обсуждении = источник жалоб, не берём
                    await bus.log(f"   🛡 @{u}: антиспам @{guard} — пропускаю")
                    continue
                good.append(f"@{u} — ~{subs} подписчиков, комменты открыты, антиспама нет")
                await asyncio.sleep(1.5)
            except Exception:
                continue
        (config.DATA_DIR / "comment_candidates.txt").write_text(
            "\n".join(good), encoding="utf-8")
        if good:
            await event.reply("🔭 Кандидаты в доноры (комменты открыты, отсортированы по размеру):\n\n"
                              + "\n".join(good[:20])
                              + "\n\nЗамени слабые каналы в data/comment_channels.txt "
                                "и перезапусти юзербот.")
        else:
            await event.reply("🔭 Новых кандидатов с открытыми комментами не нашёл.")

    # --- планировщик вечернего разбора (раз в день в COMMENT_DIGEST_TIME) ---
    async def _digest_scheduler():
        if not config.COMMENT_DIGEST_TIME:
            return
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(config.TIMEZONE)
        except Exception:
            tz = None
        from datetime import datetime
        last_run_day = None
        while True:
            await asyncio.sleep(30)
            now = datetime.now(tz)
            if now.strftime("%H:%M") == config.COMMENT_DIGEST_TIME and last_run_day != now.date():
                last_run_day = now.date()
                await _digest()

    # --- фолбэк по простою: тихо давно? предложим свежий пост сами (ротация каналов) ---
    async def _idle_watcher():
        if config.USERBOT_IDLE_MINUTES <= 0:
            return
        idx = 0
        names = [getattr(e, "username", None) for e in donor_entities]
        names = [n for n in names if n]
        while True:
            await asyncio.sleep(60)
            if time.time() - _last_offer_ts < config.USERBOT_IDLE_MINUTES * 60:
                continue
            if not names:
                continue
            # обходим каналы по кругу, ищем свежий ещё не предложенный пост
            for _ in range(len(names)):
                ch = names[idx % len(names)]
                idx += 1
                try:
                    async for m in user.iter_messages(ch, limit=8):
                        if ((m.message or "").strip() and not _seen_has(f"{ch}/{m.id}")
                                and _commentable(m)):
                            await bus.log(f"😴 Тихо {config.USERBOT_IDLE_MINUTES} мин — "
                                          f"беру свежий пост @{ch}#{m.id}")
                            await _offer(ch, m.id, m.message)
                            break
                    if time.time() - _last_offer_ts < 60:  # только что предложили — выходим
                        break
                except Exception as e:
                    await bus.log(f"   ⚠ фолбэк @{ch}: {type(e).__name__}", "warn")

    # --- разовый самотест: взять последний пост из канала и предложить сразу ---
    test_ch = config._env("USERBOT_TEST_CHANNEL")
    if test_ch:
        test_ch = test_ch.lstrip("@")
        async for m in user.iter_messages(test_ch, limit=5):
            if (m.message or "").strip():
                await bus.log(f"🧪 Самотест: беру пост @{test_ch}#{m.id}")
                await _offer(test_ch, m.id, m.message)
                break

    # --- нажатие кнопки в личке ---
    @bot.on(events.CallbackQuery())
    async def on_click(event):
        data = event.data.decode()
        if data.startswith("skip|"):
            token = data.split("|")[1]
            rec = _pending.pop(token, None)
            if rec:
                _stat("skip", rec["channel"], rec["post_id"])
            await event.edit("✖️ Пропущено.")
            return
        if data.startswith("redo|"):
            token = data.split("|")[1]
            rec = _pending.get(token)
            if not rec:
                await event.answer("Уже неактуально", alert=False)
                return
            await event.answer("Переделываю…", alert=False)
            new_v = await commenting._draft(rec["channel"], rec["text"])
            if not new_v:
                await event.answer("Не вышло, попробуй ещё раз", alert=True)
                return
            while len(new_v) < 2:
                new_v.append(new_v[0])
            rec["variants"] = new_v
            _stat("redo", rec["channel"], rec["post_id"])
            body, kb = rec["render"](token, rec["channel"], rec["post_id"],
                                     rec["text"], new_v)
            await event.edit(body, buttons=kb)
            return
        if not data.startswith("p|"):
            return
        _, token, idx = data.split("|")
        rec = _pending.get(token)
        if not rec:
            await event.answer("Уже неактуально", alert=False)
            return
        ok, why = _allowed(rec["channel"])
        if not ok:
            _stat("limit", rec["channel"], rec["post_id"], why=why)
            await event.edit(f"⏸ Не постил: {why}")
            _pending.pop(token, None)
            return

        comment = rec["variants"][int(idx)]
        _stat("approve", rec["channel"], rec["post_id"])
        delay = random.randint(config.USERBOT_MIN_DELAY_SEC, config.USERBOT_MAX_DELAY_SEC)
        await event.edit(f"⏳ Ок, запощу через ~{delay // 60} мин (по-человечески):\n\n{comment}")
        asyncio.create_task(_post_later(user, channel_entity, rec, comment, delay, token))

    searchbot.register(bot)          # публичный поисковик — всегда работает
    asyncio.create_task(_report_scheduler())  # отчёты/счётчики — всегда
    # мониторинг каналов и пачки комментов — только если комментинг включён
    if config.USERBOT_COMMENTING:
        asyncio.create_task(_idle_watcher())
        asyncio.create_task(_digest_scheduler())
        if config.USERBOT_DIGEST_ON_START:
            asyncio.create_task(_digest())
    await asyncio.gather(user.run_until_disconnected(), bot.run_until_disconnected())


async def _antispam_guard(user, ch) -> str | None:
    """Есть ли в группе обсуждения канала бот-модератор (антиспам)?
    Возвращает username бота или None. Такие группы — источник жалоб на нас:
    антиспам-бот удаляет коммент и репортит → страйк аккаунту (уже словили один)."""
    try:
        from telethon.tl.types import ChannelParticipantsAdmins
        full = await user(GetFullChannelRequest(ch))
        disc_id = getattr(full.full_chat, "linked_chat_id", None)
        if not disc_id:
            return None
        admins = await user.get_participants(disc_id, filter=ChannelParticipantsAdmins())
        for a in admins:
            if getattr(a, "bot", False):
                return getattr(a, "username", None) or "бот-админ"
        return None
    except Exception:
        return None  # не смогли проверить — не блокируем канал из-за этого


async def _join_discussion(user, ch) -> bool:
    """Вступаем в группу-обсуждение канала (нужно, если она требует членства)."""
    try:
        full = await user(GetFullChannelRequest(ch))
        disc_id = getattr(full.full_chat, "linked_chat_id", None)
        if not disc_id:
            return False
        ent = await user.get_entity(disc_id)
        await user(JoinChannelRequest(ent))
        return True
    except Exception:
        return False


async def _post_later(user, channel_entity, rec, comment, delay, token):
    """Ждём задержку и постим коммент ОТ ИМЕНИ КАНАЛА. Только send_as — иначе пропуск.
    Если группа требует вступления — вступаем и пробуем ещё раз."""
    await asyncio.sleep(delay)
    ch, post_id = rec["channel"], rec["post_id"]

    async def _send():
        return await user.send_message(ch, comment, comment_to=post_id, send_as=channel_entity)

    try:
        try:
            sent = await _send()
        except errors.ChatGuestSendForbiddenError:
            await bus.log(f"   ↩️ @{ch}: группа требует вступления — вступаю и пробую снова")
            if await _join_discussion(user, ch):
                await asyncio.sleep(5)
                sent = await _send()
            else:
                raise
        _count(ch)
        # координаты коммента — чтобы в отчёте проверить, не удалил ли его антиспам
        _stat("posted", ch, post_id,
              msg_id=getattr(sent, "id", None), chat_id=getattr(sent, "chat_id", None))
        await bus.log(f"   ✅ @{ch}#{post_id}: коммент запощен от имени канала", "success")
    except Exception as e:
        _stat("failed", ch, post_id, err=type(e).__name__)
        await bus.log(f"   ❌ @{ch}#{post_id}: не запостил "
                      f"({type(e).__name__}) — пропускаю (личный акк не палю)", "error")
    finally:
        _pending.pop(token, None)


def _alert_crash(e: Exception) -> None:
    """Шлём владельцу алерт о падении через БОТА (не через юзербот-сессию — она
    может быть мертва). Троттлинг: не чаще раза в 30 минут, иначе рестарт-цикл
    докера заспамит личку."""
    stamp = config.DATA_DIR / "userbot_alert_ts"
    try:
        if stamp.exists() and time.time() - float(stamp.read_text()) < 1800:
            return
        stamp.write_text(str(time.time()))
    except Exception:
        pass
    from . import notify
    try:
        asyncio.run(notify.owner(
            f"⚠️ ХАЛЯВА AI: юзербот упал ({type(e).__name__}: {str(e)[:200]}). "
            "Комментинг стоит — глянь логи: docker logs tg-halyava-userbot"))
    except Exception:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        _alert_crash(e)
        raise
