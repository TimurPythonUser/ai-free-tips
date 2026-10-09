"""Хранилище истории публикаций для дедупликации (простой JSON-файл)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import urlparse

from . import config


def norm_url(u: str) -> str:
    """Нормализуем ссылку, чтобы ловить дубликаты (без www, схемы, хвостового /)."""
    try:
        p = urlparse((u or "").strip())
        host = p.netloc.lower().replace("www.", "")
        path = p.path.rstrip("/").lower()
        return host + path
    except Exception:
        return (u or "").strip().lower()


def load() -> list[dict]:
    if config.HISTORY_FILE.exists():
        try:
            return json.loads(config.HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save(items: list[dict]) -> None:
    config.HISTORY_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def recently_posted_norms(days: int) -> set[str]:
    """Нормализованные ссылки, опубликованные за последние `days` дней."""
    cutoff = datetime.now() - timedelta(days=days)
    out: set[str] = set()
    for it in load():
        try:
            if datetime.fromisoformat(it["posted_at"]) >= cutoff:
                out.add(it["url_norm"])
        except Exception:
            pass
    return out


def all_posted_norms() -> set[str]:
    """ВСЕ когда-либо опубликованные ссылки (без ограничения по времени).
    Гарантирует, что та же халява (тот же URL) НИКОГДА не выйдет повторно —
    в отличие от оконного дедупа, который «забывает» старое через DEDUP_DAYS."""
    return {it.get("url_norm") for it in load() if it.get("url_norm")}


def recently_posted_titles(days: int) -> list[str]:
    """Заголовки постов за последние `days` дней — для СМЫСЛОВОГО дедупа
    (одна и та же халява с другого источника = другой URL, но тот же заголовок).
    Свежие — первыми."""
    cutoff = datetime.now() - timedelta(days=days)
    out: list[str] = []
    for it in load():
        try:
            if datetime.fromisoformat(it["posted_at"]) >= cutoff:
                title = (it.get("title") or "").strip()
                if title:
                    out.append(title)
        except Exception:
            pass
    return list(reversed(out))


# ---------- Очередь отобранных, но ещё не опубликованных халяв ----------

def load_queue() -> list[dict]:
    if config.QUEUE_FILE.exists():
        try:
            return json.loads(config.QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_queue(items: list[dict]) -> None:
    config.QUEUE_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _prune_queue(items: list[dict]) -> list[dict]:
    """Убираем протухшие (старше TTL) и уже опубликованные записи."""
    cutoff = datetime.now() - timedelta(days=config.QUEUE_TTL_DAYS)
    posted = all_posted_norms()  # бессрочно: точный повтор ссылки исключён навсегда
    out = []
    for it in items:
        try:
            queued = datetime.fromisoformat(it.get("queued_at", ""))
        except Exception:
            queued = datetime.now()
        if queued < cutoff:
            continue
        if it.get("url_norm") in posted:
            continue
        out.append(it)
    return out


def queue_len() -> int:
    return len(_prune_queue(load_queue()))


def enqueue(items: list[dict]) -> int:
    """Добавляем факты в очередь. Дедуп против самой очереди И истории публикаций.
    Возвращает число реально добавленных. Соблюдаем лимит QUEUE_MAX."""
    queue = _prune_queue(load_queue())
    have = {it.get("url_norm") for it in queue}
    posted = all_posted_norms()  # бессрочно: та же ссылка второй раз не попадёт в очередь
    added = 0
    for item in items:
        norm = norm_url(item.get("url", ""))
        if not norm or norm in have or norm in posted:
            continue
        if len(queue) >= config.QUEUE_MAX:
            break
        # выкидываем служебные поля (_date=datetime, _norm, _deep) — они не сериализуются в JSON
        rec = {k: v for k, v in item.items() if not k.startswith("_")}
        rec["url_norm"] = norm
        rec["queued_at"] = datetime.now().isoformat()
        queue.append(rec)
        have.add(norm)
        added += 1
    save_queue(queue)
    return added


def dequeue_freshest() -> dict | None:
    """Достаём из очереди самую СВЕЖУЮ по дате халяву (и удаляем её из очереди).
    Записи без даты идут после датированных."""
    queue = _prune_queue(load_queue())
    if not queue:
        save_queue(queue)
        return None

    def sort_key(it):
        d = it.get("date")
        try:
            dt = datetime.fromisoformat(str(d)[:19]) if d else datetime.min
        except Exception:
            dt = datetime.min
        return dt

    queue.sort(key=sort_key, reverse=True)
    chosen = queue.pop(0)
    save_queue(queue)
    return chosen


def add_posted(item: dict, message_id: int | None = None, text: str = "") -> None:
    items = load()
    items.append({
        "url_norm": norm_url(item.get("url", "")),
        "url": item.get("url", ""),
        "title": item.get("title", ""),
        # дата самой новости-первоисточника (для приоритета «самое свежее событие»
        # при выборе темы ролика); может отсутствовать — тогда падаем на posted_at
        "date": item.get("date"),
        # формат и полный текст — для анти-повтора и «обучения на своей истории»
        "format": item.get("format"),
        "category": item.get("category"),
        "text": text or item.get("text_body", ""),
        "message_id": message_id,
        "posted_at": datetime.now().isoformat(),
        # заготовка под будущий сбор реакций/просмотров (петля самообучения):
        # когда прикрутим чтение реакций — заполнять {"views": N, "reactions": N}
        "metrics": None,
    })
    save(items)
