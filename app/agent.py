"""Агент-оркестратор трёх ролей:
  1) РЕСЁРЧЕР  — Perplexity (2 потока) + Exa MCP, всё ПАРАЛЛЕЛЬНО, с ретраями.
  2) РЕДАКТОР  — DeepSeek: слияние, дедуп, отбор РАЗНОТЕМНЫХ сухих фактов.
  3) КРЕАТИВЩИК — DeepSeek (высокая temp): банк форматов + анти-повтор → живые посты.
Дальше: извлечение картинок и публикация в Telegram. Каждый шаг логируется в шину."""
from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx

from . import (bluesky, clients, config, creative, images, notify, prompts, search,
               store, telegram)
from .logbus import bus


def _sources(citations: list, search_results: list) -> list[dict]:
    """Собираем источники с датами: Perplexity в search_results отдаёт date каждого URL."""
    out: list[dict] = []
    seen: set[str] = set()
    for sr in search_results or []:
        if isinstance(sr, dict) and sr.get("url"):
            u = sr["url"]
            if u in seen:
                continue
            seen.add(u)
            out.append({"url": u, "date": sr.get("date"), "title": sr.get("title")})
    for u in citations or []:
        if u and u not in seen:
            seen.add(u)
            out.append({"url": u, "date": None, "title": None})
    return out


async def _log_sources(sources: list[dict], limit: int = 8) -> None:
    """Пишем в лог, что вернул Perplexity: источники с датами и ссылками."""
    for s in sources[:limit]:
        await bus.log(f"        • [{s.get('date') or 'дата?'}] {(s.get('title') or '')[:70]}  —  {s.get('url')}")
    if len(sources) > limit:
        await bus.log(f"        … ещё {len(sources) - limit} источников")


def _extract_json_items(text: str) -> list[dict]:
    """Лениво достаём массив items из ответа редактора (JSON-объект или массив)."""
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()

    a, b = text.find("["), text.rfind("]")
    if a != -1 and b != -1 and b > a:
        try:
            data = json.loads(text[a:b + 1])
            if isinstance(data, list):
                return data
        except Exception:
            pass

    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            obj = json.loads(text[s:e + 1])
            items = obj.get("items") or obj.get("candidates") or []
            if isinstance(items, list):
                return items
        except Exception:
            pass
    return []


def _parse_date(value):
    if not value or value == "null":
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(value)[:19], fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


# Домены и признаки НОВОСТНЫХ ссылок (статья про халяву, а не сама халява).
_NEWS_DOMAINS = {
    "forbes.com", "techcrunch.com", "mashable.com", "vgtimes.ru", "runet.news",
    "zamin.uz", "androidauthority.com", "gsmarena.com", "theverge.com", "engadget.com",
    "habr.com", "dzen.ru", "dzen.guru", "lenta.ru", "3dnews.ru", "cnews.ru", "rbc.ru",
    "vc.ru", "ixbt.com", "nnets.ru", "modelora.ru", "anonhaven.com", "dropweb.org",
    "reuters.com", "bloomberg.com", "cnbc.com", "businessinsider.com", "venturebeat.com",
    "arstechnica.com", "wired.com", "gizmodo.com", "zdnet.com", "tomsguide.com",
    "tadviser.ru", "hi-tech.mail.ru", "4pda.to",
}
_NEWS_PATH_MARKERS = ("/news/", "/article", "/articles/", "/blog/", "/tech/", "/press", "/story/")


def _classify_link(url: str) -> str:
    """Определяем, ведёт ли ссылка на САМУ халяву ('offer') или на НОВОСТЬ о ней ('news').
    Консервативно: при явных признаках новости → 'news' (чтобы пост не врал)."""
    try:
        p = urlparse(url)
        host = p.netloc.lower().replace("www.", "")
        path = p.path.lower()
    except Exception:
        return "news"
    if any(host == d or host.endswith("." + d) for d in _NEWS_DOMAINS):
        return "news"
    if any(m in path for m in _NEWS_PATH_MARKERS):
        return "news"
    if re.search(r"/20\d\d/", path):  # дата в пути — почти всегда статья
        return "news"
    return "offer"


# --- Смысловой дедуп по заголовку (одна халява с разных источников) ---
# Служебные слова, которые НЕ несут смысла для сравнения тем.
_TITLE_STOP = {
    "и", "в", "во", "на", "по", "от", "до", "для", "с", "со", "к", "у", "за", "из",
    "the", "a", "an", "of", "to", "for", "in", "on", "free", "now",
    "бесплатно", "бесплатный", "бесплатная", "бесплатные", "бесплатных", "бесплатным",
    "доступ", "халява", "халяву", "новый", "новая", "новые", "ии", "ai", "нейросеть",
    "нейросети", "нейросетей", "сейчас", "уже",
}


def _title_tokens(title: str) -> set[str]:
    """Значимые ОСНОВЫ слов заголовка: без тегов, пунктуации, эмодзи и стоп-слов.
    Грубый стемминг (обрезка до 5 символов) сглаживает русскую морфологию:
    «хранилищем»/«хранилища», «удвоенным»/«удвоением» → одна основа."""
    t = re.sub(r"<[^>]+>", " ", title or "")
    t = re.sub(r"[^0-9a-zA-Zа-яёА-ЯЁ\s]", " ", t).lower()
    return {w[:5] for w in t.split() if len(w) > 2 and w not in _TITLE_STOP}


def _is_semantic_dup(title: str, posted_titles: list[str]) -> str | None:
    """Похож ли заголовок на что-то УЖЕ опубликованное? Возвращает совпавший
    заголовок или None. Консервативно: ловим явные пересечения тем."""
    a = _title_tokens(title)
    if len(a) < 2:
        return None
    for pt in posted_titles:
        b = _title_tokens(pt)
        if len(b) < 2:
            continue
        inter = len(a & b)
        union = len(a | b)
        # сильное перекрытие ИЛИ одно множество почти целиком внутри другого
        if union and inter / union >= 0.5:
            return pt
        if inter >= 3 and (inter >= len(a) - 1 or inter >= len(b) - 1):
            return pt
    return None


def _select(candidates: list[dict]) -> list[dict]:
    """Дедуп + отбор. Уровни ссылок: сначала глубокие (страница акции),
    потом добор главными страницами сервисов. Внутри уровня — свежее сверху."""
    recent = store.all_posted_norms()  # бессрочно: та же ссылка повторно не выйдет
    posted_titles = store.recently_posted_titles(config.DEDUP_DAYS)  # смысловой — в окне

    seen: set[str] = set()
    uniq: list[dict] = []
    for c in candidates:
        url = (c.get("url") or "").strip()
        if not url or not url.lower().startswith("http"):
            continue
        c["_norm"] = store.norm_url(url)
        c["_date"] = _parse_date(c.get("date"))
        c["_deep"] = bool(urlparse(url).path.strip("/"))
        c["link_type"] = _classify_link(url)   # 'offer' | 'news' — для честной подачи
        if not c.get("title"):
            continue
        if c["_norm"] in seen:
            continue
        seen.add(c["_norm"])
        uniq.append(c)

    # отсекаем по URL И по смыслу (та же халява с другого сайта)
    new = []
    for c in uniq:
        if c["_norm"] in recent:
            continue
        dup = _is_semantic_dup(c.get("title", ""), posted_titles)
        if dup:
            continue
        new.append(c)
    fresh_cut = datetime.now() - timedelta(days=config.FRESH_DAYS)

    def by_freshness(pool: list[dict]) -> list[dict]:
        dated_fresh = sorted(
            [c for c in pool if c["_date"] and c["_date"] >= fresh_cut],
            key=lambda c: c["_date"], reverse=True,
        )
        rest = sorted(
            [c for c in pool if c not in dated_fresh],
            key=lambda c: (c["_date"] or datetime.min), reverse=True,
        )
        return dated_fresh + rest

    deep = by_freshness([c for c in new if c["_deep"]])
    home = by_freshness([c for c in new if not c["_deep"]])

    return (deep + home)[: config.POST_COUNT]


def _count_fresh_sources(blocks: list[dict]) -> int:
    """Сколько УНИКАЛЬНЫХ источников со свежей датой (в окне) дали поисковые движки.
    По этому числу решаем, звать ли Perplexity (ленивый fallback, вариант А)."""
    cut = datetime.now() - timedelta(days=config.FRESH_SOURCE_WINDOW_DAYS)
    seen: set[str] = set()
    for b in blocks:
        for s in b.get("sources", []) or []:
            url = s.get("url")
            if not url:
                continue
            d = _parse_date(s.get("date"))
            if d and d >= cut:
                seen.add(store.norm_url(url))
    return len(seen)


async def _research() -> list[dict]:
    """РЕСЁРЧЕР (вариант А): сначала параллельно Tavily + Exa (прямой HTTP).
    Если свежих источников достаточно — Perplexity НЕ зовём. Иначе добираем им."""

    async def tavily() -> dict | None:
        if not config.TAVILY_API_KEY:
            await bus.log("🔎 [Tavily] TAVILY_API_KEY не задан — поток пропущен", "warn")
            return None
        text, src = await search.tavily_search(prompts.SEARCH_QUERY)
        await _log_sources(src)
        return {"label": "поиск Tavily (RU, HTTP)", "text": text, "sources": src}

    async def tavily_en() -> dict | None:
        """Английский поток: первоисточники публикуют халявы раньше пересказов."""
        if not config.TAVILY_API_KEY:
            return None
        text, src = await search.tavily_search(prompts.SEARCH_QUERY_EN)
        await _log_sources(src)
        return {"label": "поиск Tavily (EN, HTTP)", "text": text, "sources": src}

    async def exa() -> dict | None:
        if not config.EXA_API_KEY:
            await bus.log("🔎 [Exa] EXA_API_KEY не задан — поток пропущен", "warn")
            return None
        # Exa силён в англоязычном вебе — ищем на английском: свежие халявы
        # анонсируются там раньше, чем доходят до русских пересказов
        text, src = await search.exa_search(prompts.SEARCH_QUERY_EN)
        await _log_sources(src)
        return {"label": "поиск Exa (EN, HTTP)", "text": text, "sources": src}

    async def grants() -> dict | None:
        """Доп. поток «гранты для ИИ-стартапов» — только в GRANTS_WEEKDAYS.
        Вливается в общий пул кандидатов, дальше отбирает редактор как обычно."""
        if datetime.now().isoweekday() not in config.GRANTS_WEEKDAYS:
            return None
        if not config.TAVILY_API_KEY:
            return None
        await bus.log("🎓 [Гранты] сегодня грантовый день — ищу гранты для ИИ-стартапов…")
        text, src = await search.tavily_search(prompts.GRANTS_SEARCH_QUERY)
        await _log_sources(src)
        return {"label": "поиск грантов для ИИ-стартапов (EN, Tavily)", "text": text, "sources": src}

    def _collect(results) -> list[dict]:
        out: list[dict] = []
        for r in results:
            if isinstance(r, dict):
                out.append(r)
            elif isinstance(r, Exception):
                # None (пропущенный поток) сюда не попадает — он не Exception
                pass
        return out

    # --- Первый план: Tavily RU+EN + Exa EN (+ гранты по расписанию) параллельно ---
    await bus.log("🔎 РЕСЁРЧЕР: первый план — Tavily RU+EN + Exa EN (прямой HTTP), параллельно…")
    primary = await asyncio.gather(tavily(), tavily_en(), exa(), grants(),
                                   return_exceptions=True)
    for r in primary:
        if isinstance(r, Exception):
            await bus.log(f"   ⚠️ поток поиска упал: {type(r).__name__}: {r}", "warn")
    blocks = _collect(primary)
    # англоязычные потоки — первыми в промпт редактора: первоисточники свежее
    # и первичнее пересказов, редактор начинает отбор с них
    blocks.sort(key=lambda b: 0 if "EN" in (b.get("label") or "") else 1)

    # --- Ленивый Perplexity: только если свежего мало ---
    fresh = _count_fresh_sources(blocks)
    need = config.PERPLEXITY_FALLBACK_MIN_FRESH
    if fresh >= need:
        await bus.log(f"✅ Движки дали {fresh} свежих источников (порог {need}) — Perplexity пропускаю (экономлю).")
    else:
        await bus.log(f"↩️ Свежих источников {fresh} < порога {need} — добираю Perplexity (2 потока).")
        blocks += await _pplx_blocks()

    return blocks


async def _pplx_blocks() -> list[dict]:
    """Два глубоких потока Perplexity. Пусто — если нет ключа или всё упало."""
    if not config.PERPLEXITY_API_KEY:
        await bus.log("🌐 [Perplexity] ключ не задан — глубокий поиск недоступен", "warn")
        return []

    async def pplx(label: str, query: str) -> dict:
        await bus.log(f"🌐 [Perplexity] {label}")
        await bus.log(f"   ➡️ POST {config.PERPLEXITY_BASE_URL}/chat/completions  (модель {config.PERPLEXITY_MODEL})")
        c, cit, sr = await clients.perplexity(query, prompts.PPLX_SYSTEM)
        src = _sources(cit, sr)
        await bus.log(f"   ⬅️ [{label}] {len(src)} источников (с датами: {sum(1 for s in src if s['date'])})")
        await _log_sources(src)
        return {"label": label, "text": c, "sources": src}

    results = await asyncio.gather(
        pplx("ПОТОК доступ — платные ИИ, акции/скидки, новинки с бесплатным", prompts.PPLX_ACCESS_QUERY),
        pplx("ПОТОК инструменты — новинки, превью, конкурсы, гранты, курсы", prompts.PPLX_TOOLS_QUERY),
        pplx("ПОТОК EN — глобальные анонсы из первоисточников", prompts.PPLX_EN_QUERY),
        return_exceptions=True,
    )
    out: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            await bus.log(f"   ⚠️ поток Perplexity упал: {type(r).__name__}: {r}", "warn")
        elif isinstance(r, dict):
            out.append(r)
    return out


async def _edit(blocks: list[dict]) -> list[dict]:
    """РЕДАКТОР: слияние и отбор сухих фактов (DeepSeek, низкая temp)."""
    await bus.log("🧠 [Редактор] отбор — объединяю кандидатов, чищу дубли, сортирую по свежести")
    posted_titles = store.recently_posted_titles(config.DEDUP_DAYS)
    if posted_titles:
        await bus.log(f"   🚫 антиповтор: передаю редактору {len(posted_titles)} уже опубликованных заголовков")
    user_prompt = prompts.build_merge_prompt(blocks, posted_titles)
    total_sources = sum(len(b.get("sources", [])) for b in blocks)
    await bus.log(f"   ➡️ POST {config.DEEPSEEK_BASE_URL}/chat/completions  (модель {config.DEEPSEEK_MODEL}, temp {config.EDITOR_TEMPERATURE})")
    await bus.log(f"   ➡️ отправляю промпт: {len(user_prompt)} символов, {total_sources} источников (потоков: {len(blocks)})")
    raw = await clients.deepseek(
        [
            {"role": "system", "content": prompts.EDITOR_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        temperature=config.EDITOR_TEMPERATURE,
    )
    candidates = _extract_json_items(raw)
    await bus.log(f"   ⬅️ ответ Редактора: {len(raw or '')} символов → {len(candidates)} фактов")
    for c in candidates:
        await bus.log(f"        • [{c.get('date') or 'дата?'}] {c.get('title', '')}  —  {c.get('url', '')}")
    if not candidates:
        try:
            (config.DATA_DIR / "last_deepseek.txt").write_text(raw or "(пусто)", encoding="utf-8")
        except Exception:
            pass
        await bus.log(f"Сырой ответ Редактора (первые 400 символов): {(raw or '')[:400]!r}", "error")
    return candidates


async def harvest() -> dict:
    """ДОБЫТЧИК: ресёрч → редактор → отбор → кладём в очередь. Ничего НЕ публикует.
    Запускается по расписанию раз в день (worker.py)."""
    if bus.status.get("running"):
        await bus.log("Агент уже выполняется — дождись завершения.", "warn")
        return {"found": 0, "enqueued": 0}
    missing = config.missing_keys()
    if missing:
        await bus.log(f"Не заполнены ключи в .env: {', '.join(missing)}", "error")
        return {"found": 0, "enqueued": 0}

    bus.set_status(running=True)
    try:
        await bus.log("🌾 ДОБЫТЧИК: ищу халявы и кладу в очередь (без публикации)…")
        blocks = await _research()
        if not blocks:
            await bus.log("Ни один поток поиска не вернул результат.", "error")
            return {"found": 0, "enqueued": 0}

        candidates = await _edit(blocks)
        if not candidates and not any("ПОТОК" in (b.get("label") or "") for b in blocks):
            # Редактор всё забраковал (обычно всё — дубли уже вышедшего), а глубокий
            # поиск Perplexity ещё не задействован — докидываем его и пробуем снова.
            await bus.log("↩️ Редактор забраковал всё из быстрых движков — добираю Perplexity и пробую ещё раз.")
            extra = await _pplx_blocks()
            if extra:
                candidates = await _edit(blocks + extra)
        if not candidates:
            await bus.log("Не удалось извлечь факты из ответа Редактора.", "error")
            await notify.owner("⚠️ ХАЛЯВА AI: добыча не нашла НИ ОДНОЙ новой халявы "
                               "(даже с Perplexity). Очередь могла не пополниться — "
                               "глянь логи на сервере.")
            return {"found": 0, "enqueued": 0}

        selected = _select(candidates)
        if not selected and not any("ПОТОК" in (b.get("label") or "") for b in blocks):
            # Кандидаты были, но отбор всё выкинул как дубли уже вышедшего —
            # глубокий Perplexity ещё не звали: добираем им и отбираем заново.
            await bus.log("↩️ Отбор выкинул всё как дубли — добираю Perplexity и пробую ещё раз.")
            extra = await _pplx_blocks()
            if extra:
                candidates = await _edit(blocks + extra)
                selected = _select(candidates)
        enq = store.enqueue(selected)
        await bus.log(
            f"🌾 Добыто {len(selected)}, добавлено в очередь {enq} "
            f"(в очереди теперь {store.queue_len()}).", "success"
        )
        # тревожим владельца, только если пополнения нет И запас реально тает
        # (вторая добыча за день часто находит те же новости, что первая — это норма)
        if not enq and store.queue_len() < 3:
            await notify.owner(f"⚠️ ХАЛЯВА AI: новых халяв не нашлось (всё дубли), "
                               f"а в очереди осталось всего {store.queue_len()} — "
                               "скоро может быть пустой слот.")
        return {"found": len(selected), "enqueued": enq}
    except Exception as e:
        await bus.log(f"Фатальная ошибка добычи: {type(e).__name__}: {e}", "error")
        return {"found": 0, "enqueued": 0}
    finally:
        bus.set_status(running=False, last_harvest=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def _clip_sentence(text: str, limit: int) -> str:
    """Обрезаем текст до limit символов, но по границе предложения, а не посреди слова."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "),
              cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if end > limit * 0.5:
        return cut[:end + 1].strip()
    return cut.rsplit(" ", 1)[0].rstrip() + "…"


def _threads_text(item: dict) -> str:
    """Короткий пост для Threads: текст (по границе предложения) + ссылка на источник
    + ссылка-счётчик «Threads». Всё целиком влезает в лимит Threads (500 симв)."""
    raw = item.get("text_body") or item.get("text") or item.get("title") or ""
    plain = re.sub(r"<[^>]+>", "", raw)
    plain = re.sub(r"\s+", " ", plain).strip()
    plain = _clip_sentence(plain, 200)  # оставляем место под две ссылки

    tg = ""
    try:
        if config.INVITE_LINKS_FILE.exists():
            tg = json.loads(
                config.INVITE_LINKS_FILE.read_text(encoding="utf-8")).get("threads", "")
    except Exception:
        pass
    tg = tg or f"https://t.me/{config.TELEGRAM_CHANNEL.lstrip('@')}"

    lines = [plain]
    url = (item.get("url") or "").strip()
    if url:
        lines.append(f"🔗 Источник: {url}")
    lines.append(f"📢 Все халявы: {tg}")
    return "\n\n".join(lines)


async def publish_one() -> bool:
    """ПУБЛИКАТОР: берёт из очереди самую свежую халяву, пишет пост и постит.
    Запускается по расписанию в каждый слот (worker.py)."""
    missing = config.missing_keys()
    if missing:
        await bus.log(f"Не заполнены ключи в .env: {', '.join(missing)}", "error")
        return False

    item = store.dequeue_freshest()
    if not item:
        await bus.log("📭 Очередь пуста — публиковать нечего, пропускаю слот.", "warn")
        await notify.owner("⚠️ ХАЛЯВА AI: слот публикации пропущен — очередь пуста. "
                           "Пост в канал НЕ вышел.")
        return False

    link_kind = "🔗 прямая (сама халява)" if item.get("link_type") == "offer" else "📰 новость/источник"
    await bus.log(f"📣 ПУБЛИКАТОР: беру из очереди — {item.get('title', '(без названия)')}")
    await bus.log(f"      дата: {item.get('date') or 'неизвестна'} | источник: {item.get('source') or '—'} | категория: {item.get('category') or '—'}")
    await bus.log(f"      ссылка: {item.get('url')}  [{link_kind}] | осталось в очереди: {store.queue_len()}")

    await bus.log(f"      🖼 [HTTP] GET {item.get('url')} — ищу оригинальный og:image")
    img = await images.get_original_image(item["url"])
    if img:
        item["image"] = img
        await bus.log(f"      🖼 [HTTP] найден og:image: {img}")
    else:
        await bus.log("      🖼 [HTTP] og:image не найден — публикую текстом со ссылкой")

    # креатив: формат и анти-повтор считаем от РЕАЛЬНОЙ истории (посты разнесены во времени)
    fmt = creative.pick_formats(1, creative.recent_formats(config.ANTIREPEAT_LOOKBACK))[0]
    openers = creative.recent_openers(config.ANTIREPEAT_LOOKBACK)
    item = await creative.write_post(item, fmt, openers)

    preview = re.sub(r"<[^>]+>", "", telegram._caption(item, 1024))
    await bus.log("      📝 текст поста:\n" + preview)

    mode_plan = "sendPhoto (фото+подпись)" if img else "sendMessage (текст+ссылка)"
    await bus.log(f"      📤 [Telegram] {mode_plan} → {config.TELEGRAM_CHANNEL}")
    ok, mode, message_id = await telegram.post(item)
    if ok:
        store.add_posted(item, message_id, text=item.get("text_body", ""))
        await bus.log(f"      ⬅️ [Telegram] ok, message_id={message_id} (режим: {mode})", "success")
        # (Threads-текст отключён — Threads в бане; TikTok на паузе. Оставили YouTube.)
        # Bluesky: автопост текстом + картинкой (полный автомат, к ботам лоялен)
        try:
            if bluesky._enabled():
                link = bluesky.invite_link()
                body = _clip_sentence(
                    re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", item.get("text_body") or "")), 210)
                body = await bluesky.translate(body)  # Bluesky — на английском
                txt = f"{body}\n\n🎁 {link}"
                img = None
                if item.get("image"):
                    async with httpx.AsyncClient(timeout=30) as ic:
                        ir = await ic.get(item["image"])
                        if ir.status_code == 200 and len(ir.content) < 900_000:
                            img = ir.content
                await bluesky.post_text(txt, url=link, image=img)
        except Exception:
            pass  # Bluesky не должен ронять публикацию
        return True

    await bus.log(f"      ❌ [Telegram] ошибка публикации: {mode} — возвращаю халяву в очередь", "error")
    store.enqueue([item])   # не теряем: попробуем в следующий слот
    return False


async def run() -> dict:
    """Ручной полный цикл (для локальной веб-панели): добыть и сразу опубликовать
    до POST_COUNT постов пачкой. На сервере НЕ используется — там worker.py по расписанию."""
    result = {"posted": 0, "items": []}
    await harvest()
    n = min(config.POST_COUNT, store.queue_len())
    for _ in range(n):
        if await publish_one():
            result["posted"] += 1
    await bus.log(f"🏁 Готово (ручной цикл). Опубликовано: {result['posted']}.", "success")
    bus.set_status(last_run=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), last_result=result)
    return result
