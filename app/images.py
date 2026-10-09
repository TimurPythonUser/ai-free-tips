"""Извлечение ОРИГИНАЛЬНОЙ картинки со страницы акции (og:image / twitter:image)."""
from __future__ import annotations

import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AiFreeTipsBot/1.0)"}
_META_PROPS = ["og:image", "og:image:url", "og:image:secure_url",
               "twitter:image", "twitter:image:src"]


async def get_original_image(url: str) -> str | None:
    """Возвращает URL оригинальной картинки со страницы или None, если её нет."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_HEADERS) as c:
            r = await c.get(url)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for prop in _META_PROPS:
                tag = (soup.find("meta", property=prop)
                       or soup.find("meta", attrs={"name": prop}))
                if tag and tag.get("content"):
                    img = urljoin(url, tag["content"].strip())
                    if img.lower().startswith("http"):
                        return img
    except Exception:
        return None
    return None
