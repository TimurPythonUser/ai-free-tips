"""Комментинг-помощник: следит за чужими Telegram-каналами и присылает владельцу
в личку черновики комментариев под свежие посты.

Как читаем чужие каналы БЕЗ юзербота: у публичных каналов есть веб-превью
t.me/s/<username> — обычная HTML-страница с последними постами. Парсим её.
Никакой автопостинг комментариев не делается принципиально: пишет живой человек,
иначе бан и ноль профита. Мы экономим время на мониторинге и черновике.
"""
from __future__ import annotations

import html
import json
import re

import httpx
from bs4 import BeautifulSoup

from . import clients, config
from .clients import _with_retries
from .logbus import bus

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_COMMENT_SYSTEM = """Ты — практикующий специалист по ИИ и разработке. Ты читаешь телеграм-каналы
про ИИ/IT и оставляешь комментарии от имени профильного канала. Твой стиль — умный,
спокойный, профессиональный: по делу, с аргументами, без воды и без фамильярности.

ТВОЯ ЗАДАЧА: выдать ДВА РАЗНЫХ по подходу комментария к посту.

Палитра подходов (выбери два разных, которые подходят к конкретному посту):
- умный уточняющий вопрос по сути (метрики, лицензия, лимиты, приватность данных,
  чем отличается от аналогов, как поведёт себя на реальной нагрузке)
- вежливое указание на слабое место, ограничение или подводный камень — с аргументом
- дополнение по опыту: практическая деталь, о которой в посте не сказано
- обоснованный скепсис: что именно вызывает сомнение и почему
- сдержанное признание сильной стороны + конкретика, почему это действительно важно
- сравнение с альтернативой и в чём принципиальная разница

Требования к стилю:
- Грамотный живой русский. БЕЗ сленга, БЕЗ жаргона, БЕЗ мата.
- Вежливо и уважительно, даже когда критикуешь: критика — к продукту или утверждению,
  никогда не к автору поста.
- Каждый тезис подкрепляй коротким аргументом («потому что…», «судя по документации…»,
  «в похожем кейсе…»). Голословных наездов не делать.
- 1–3 коротких предложения. Строго по теме конкретного поста, не общими словами.
- Эмодзи: максимум 1 и только если действительно уместно; чаще — вообще без эмодзи.
- Никаких ссылок, рекламы, «подпишись», упоминаний своего канала.
- Без подхалимства («отличный пост!», «спасибо за инфу»), без канцелярита,
  без фраз-заготовок, которые подошли бы к любому посту.

Верни СТРОГО JSON: {"variants": ["вариант 1", "вариант 2"]}"""


# ---------- список каналов и состояние ----------

def channels() -> list[str]:
    if not config.COMMENT_CHANNELS_FILE.exists():
        return []
    out = []
    for line in config.COMMENT_CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip().lstrip("@")
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _state_load() -> dict:
    if config.COMMENT_STATE_FILE.exists():
        try:
            return json.loads(config.COMMENT_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _state_save(state: dict) -> None:
    config.COMMENT_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------- чтение канала через t.me/s ----------

async def _fetch_posts(channel: str) -> list[dict]:
    """Последние посты канала: [{id, text}]. Пусто — если превью недоступно."""
    url = f"https://t.me/s/{channel}"

    async def _get():
        async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                     headers={"User-Agent": _UA}) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.text

    html_text = await _with_retries(_get, f"t.me/s/{channel}")
    soup = BeautifulSoup(html_text, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        data_post = msg.get("data-post", "")
        m = re.match(rf"{re.escape(channel)}/(\d+)", data_post, re.I)
        if not m:
            continue
        text_el = msg.select_one("div.tgme_widget_message_text")
        text = text_el.get_text(" ", strip=True) if text_el else ""
        if text:
            posts.append({"id": int(m.group(1)), "text": text})
    posts.sort(key=lambda p: p["id"])
    return posts


# ---------- черновик комментария ----------

def _parse_variants(raw: str) -> list[str]:
    raw = (raw or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if m:
        raw = m.group(1).strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e != -1:
        try:
            data = json.loads(raw[s:e + 1])
            v = [str(x).strip() for x in (data.get("variants") or []) if str(x).strip()]
            if v:
                return v[:2]
        except Exception:
            pass
    return []


async def _draft(channel: str, post_text: str) -> list[str]:
    raw = await clients.deepseek(
        [{"role": "system", "content": _COMMENT_SYSTEM},
         {"role": "user", "content": f"Пост из канала @{channel}:\n\n{post_text[:1500]}"}],
        temperature=config.COMMENT_TEMPERATURE,
        model=config.CREATIVE_MODEL,
    )
    return _parse_variants(raw)


# ---------- отправка владельцу ----------

async def _send_owner(channel: str, post_id: int, post_text: str, variants: list[str]) -> bool:
    excerpt = post_text[:300] + ("…" if len(post_text) > 300 else "")
    parts = [
        f"💬 <b>Новый пост в @{channel}</b>",
        f"<i>{html.escape(excerpt)}</i>",
        f"🔗 https://t.me/{channel}/{post_id}",
        "<b>Черновики коммента</b> (подправь под себя и запость от имени канала):",
    ]
    parts += [f"<pre>{html.escape(v)}</pre>" for v in variants]
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.OWNER_CHAT_ID, "text": "\n\n".join(parts),
                  "parse_mode": "HTML", "disable_web_page_preview": True},
        )
        return r.status_code == 200 and r.json().get("ok", False)


# ---------- основной прогон ----------

async def check_once() -> None:
    """Обходим каналы-доноры; на новые посты шлём владельцу черновики комментов.
    Первый прогон по каналу только запоминает последний пост (без спама)."""
    if not config.OWNER_CHAT_ID:
        await bus.log("💬 OWNER_CHAT_ID не задан — комментинг-помощнику некуда слать", "error")
        return
    chans = channels()
    if not chans:
        await bus.log(f"💬 Список каналов пуст — впиши @каналы в {config.COMMENT_CHANNELS_FILE.name}", "warn")
        return

    await bus.log(f"💬 Комментинг: проверяю {len(chans)} канал(ов)…")
    state = _state_load()
    sent = 0
    for ch in chans:
        try:
            posts = await _fetch_posts(ch)
        except Exception as e:
            await bus.log(f"   ⚠ @{ch}: превью недоступно ({type(e).__name__})", "warn")
            continue
        if not posts:
            await bus.log(f"   ⚠ @{ch}: постов не видно (закрытый канал или пустое превью)", "warn")
            continue

        last_seen = state.get(ch)
        newest = posts[-1]["id"]
        if last_seen is None:
            state[ch] = newest
            await bus.log(f"   👀 @{ch}: инициализировал (последний пост #{newest})")
            continue

        fresh = [p for p in posts if p["id"] > last_seen]
        state[ch] = newest
        if not fresh:
            await bus.log(f"   ✔ @{ch}: нового нет")
            continue

        post = fresh[-1]  # берём самый свежий, чтобы не заваливать личку
        await bus.log(f"   ✨ @{ch}: новый пост #{post['id']} — пишу черновик…")
        variants = await _draft(ch, post["text"])
        if not variants:
            await bus.log(f"   ⚠ @{ch}: креативщик не выдал варианты", "warn")
            continue
        if await _send_owner(ch, post["id"], post["text"], variants):
            sent += 1
            await bus.log(f"   📨 @{ch}: черновик отправлен в личку", "success")
        if sent >= config.COMMENT_MAX_PER_RUN:
            await bus.log(f"   ⏸ лимит {config.COMMENT_MAX_PER_RUN} за прогон — остальное в следующий раз")
            break

    _state_save(state)
    await bus.log(f"💬 Комментинг: готово, черновиков отправлено: {sent}", "success")
