"""Фоновые вертикальные клипы с Pexels (бесплатный API, https://www.pexels.com/api/).
Скачанные клипы кэшируются в data/stock/. Нет ключа или ничего не нашлось — вернём
пустой список, и сборщик нарисует градиентный фон."""
from __future__ import annotations

from pathlib import Path

import httpx

from .. import config
from ..clients import _with_retries
from ..logbus import bus

_SEARCH_URL = "https://api.pexels.com/videos/search"


def _pick_file(video: dict) -> dict | None:
    """Выбираем вертикальный файл поменьше весом, но не ниже 1080 по высоте."""
    candidates = [
        f for f in video.get("video_files", [])
        if (f.get("height") or 0) >= 1080 and (f.get("height") or 0) > (f.get("width") or 0)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda f: f.get("height") or 10**9)


async def fetch_clips(keywords: list[str], want: int = 6) -> list[Path]:
    """До `want` вертикальных клипов по ключевым словам. Пути к локальным файлам."""
    if not config.PEXELS_API_KEY:
        await bus.log("      ℹ PEXELS_API_KEY не задан — фон будет градиентным")
        return []

    query = " ".join(keywords[:3]) or "technology neon abstract"
    headers = {"Authorization": config.PEXELS_API_KEY}
    params = {"query": query, "orientation": "portrait", "per_page": want * 2, "size": "medium"}

    async def _search():
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(_SEARCH_URL, headers=headers, params=params)
            r.raise_for_status()
            return r.json()

    try:
        data = await _with_retries(_search, "Pexels search")
    except Exception as e:
        await bus.log(f"      ⚠ Pexels недоступен ({type(e).__name__}) — фон будет градиентным", "warn")
        return []

    clips: list[Path] = []
    for video in data.get("videos", []):
        if len(clips) >= want:
            break
        f = _pick_file(video)
        if not f or not f.get("link"):
            continue
        path = config.STOCK_DIR / f"pexels_{video['id']}_{f.get('height')}.mp4"
        if path.exists() and path.stat().st_size > 0:
            clips.append(path)
            continue
        try:
            async def _download(url=f["link"], dest=path):
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
                    r = await c.get(url)
                    r.raise_for_status()
                    dest.write_bytes(r.content)

            await _with_retries(_download, "Pexels download")
            clips.append(path)
            await bus.log(f"      📥 сток: скачал клип #{video['id']}")
        except Exception as e:
            await bus.log(f"      ⚠ клип #{video['id']} не скачался ({type(e).__name__})", "warn")

    await bus.log(f"      🎞 сток по запросу «{query}»: {len(clips)} клип(а)")
    return clips
