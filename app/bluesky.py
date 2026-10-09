"""Автопостинг в Bluesky (AT Protocol) — БЕЗ SDK, на чистом httpx.

Bluesky к ботам лоялен: логинимся app-паролем, постим текст с кликабельной
ссылкой-счётчиком (+ картинка), а ролики — видео-постом. Всё автоматически.

Тихо молчит и не роняет основной процесс, если ключей нет или API недоступен.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import config
from .logbus import bus

_PDS = "https://bsky.social"
_VIDEO_HOST = "https://video.bsky.app"


def _enabled() -> bool:
    return bool(config.BLUESKY_HANDLE and config.BLUESKY_APP_PASSWORD)


async def translate(text_ru: str) -> str:
    """Русский текст → цепкий английский (Bluesky в основном англоязычный).
    Ответ обёрнут в <t>…</t> — выдираем только его, любые «размышления» модели
    (deepseek-v4-pro иногда рассуждает вслух) отсекаются. Сбой → оригинал."""
    import re as _re
    from . import clients
    if not (text_ru or "").strip():
        return text_ru
    try:
        out = await clients.deepseek(
            [{"role": "system", "content":
              "You translate Russian social-media posts about free AI tools into "
              "natural, catchy English (under 240 characters, no hashtags, no links). "
              "Output ONLY the final translation wrapped exactly as <t>translation</t>. "
              "No reasoning, no notes, nothing outside the tags."},
             {"role": "user", "content": text_ru}],
            temperature=0.0)
        m = _re.search(r"<t>(.*?)</t>", out or "", _re.S)
        if m:
            res = m.group(1).strip().strip('"').strip()
            if res:
                return res[:280]
        # тегов нет — берём последнюю осмысленную строку без рассуждений
        for line in reversed((out or "").splitlines()):
            s = line.strip().strip('"').strip()
            if s and len(s) < 280 and not s.lower().startswith(("so ", "the translation",
                    "i think", "maybe", "intended", "note")):
                return s
        return text_ru
    except Exception:
        return text_ru


async def translate_long(text_ru: str) -> str:
    """Полный перевод текста озвучки ролика → английский (для англоязычного
    ролика в Bluesky). В отличие от translate() без лимита длины — нужен весь
    текст целиком, иначе озвучка оборвётся. Сбой → оригинал."""
    import re as _re
    from . import clients
    if not (text_ru or "").strip():
        return text_ru
    try:
        out = await clients.deepseek(
            [{"role": "system", "content":
              "You translate a short Russian voiceover script about a free AI tool "
              "into natural spoken English. Keep it punchy and conversational, same "
              "meaning and sentence order, no hashtags, no links, no emoji. "
              "Output ONLY the translation wrapped exactly as <t>translation</t>. "
              "No reasoning, no notes, nothing outside the tags."},
             {"role": "user", "content": text_ru}],
            temperature=0.0)
        m = _re.search(r"<t>(.*?)</t>", out or "", _re.S)
        if m:
            res = _re.sub(r"\s+", " ", m.group(1).strip().strip('"').strip())
            if res:
                return res[:1500]
        # тегов нет — берём самую длинную осмысленную строку без рассуждений
        cands = [l.strip().strip('"').strip() for l in (out or "").splitlines()]
        cands = [s for s in cands if s and not s.lower().startswith(
            ("so ", "the translation", "i think", "maybe", "intended", "note", "okay"))]
        if cands:
            return max(cands, key=len)[:1500]
        return text_ru
    except Exception:
        return text_ru


def invite_link() -> str:
    """Ссылка-счётчик Bluesky (или общая на канал, если счётчик ещё не создан)."""
    try:
        if config.INVITE_LINKS_FILE.exists():
            link = json.loads(
                config.INVITE_LINKS_FILE.read_text(encoding="utf-8")).get("bluesky")
            if link:
                return link
    except Exception:
        pass
    return f"https://t.me/{config.TELEGRAM_CHANNEL.lstrip('@')}"


async def _session(c: httpx.AsyncClient) -> dict | None:
    """Логин app-паролем → {accessJwt, did}. None при ошибке."""
    try:
        r = await c.post(f"{_PDS}/xrpc/com.atproto.server.createSession",
                         json={"identifier": config.BLUESKY_HANDLE,
                               "password": config.BLUESKY_APP_PASSWORD})
        r.raise_for_status()
        d = r.json()
        return {"jwt": d["accessJwt"], "did": d["did"]}
    except Exception as e:
        await bus.log(f"      ⚠ Bluesky: логин не удался ({type(e).__name__})", "warn")
        return None


def _link_facets(text: str, url: str) -> list[dict]:
    """Делаем ссылку в тексте кликабельной (facet по UTF-8 байтам)."""
    pos = text.find(url)
    if pos < 0:
        return []
    start = len(text[:pos].encode("utf-8"))
    end = start + len(url.encode("utf-8"))
    return [{"index": {"byteStart": start, "byteEnd": end},
             "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]}]


async def _upload_blob(c: httpx.AsyncClient, sess: dict, data: bytes,
                       mime: str) -> dict | None:
    try:
        r = await c.post(f"{_PDS}/xrpc/com.atproto.repo.uploadBlob", content=data,
                         headers={"Authorization": f"Bearer {sess['jwt']}",
                                  "Content-Type": mime})
        r.raise_for_status()
        return r.json()["blob"]
    except Exception as e:
        await bus.log(f"      ⚠ Bluesky: не загрузился блоб ({type(e).__name__})", "warn")
        return None


async def _create(c: httpx.AsyncClient, sess: dict, record: dict) -> bool:
    try:
        r = await c.post(f"{_PDS}/xrpc/com.atproto.repo.createRecord",
                         headers={"Authorization": f"Bearer {sess['jwt']}"},
                         json={"repo": sess["did"], "collection": "app.bsky.feed.post",
                               "record": record})
        r.raise_for_status()
        return True
    except Exception as e:
        detail = ""
        try:
            detail = r.text[:200]
        except Exception:
            pass
        await bus.log(f"      ⚠ Bluesky: пост не создан ({type(e).__name__}) {detail}", "warn")
        return False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


async def post_text(text: str, url: str | None = None,
                    image: bytes | None = None) -> bool:
    """Текстовый пост (+ кликабельная ссылка + опц. картинка)."""
    if not _enabled():
        return False
    async with httpx.AsyncClient(timeout=60) as c:
        sess = await _session(c)
        if not sess:
            return False
        record = {"$type": "app.bsky.feed.post", "text": text[:300], "createdAt": _now()}
        if url:
            facets = _link_facets(text[:300], url)
            if facets:
                record["facets"] = facets
        if image:
            blob = await _upload_blob(c, sess, image, "image/jpeg")
            if blob:
                record["embed"] = {"$type": "app.bsky.embed.images",
                                   "images": [{"alt": "ИИ-халява", "image": blob}]}
        ok = await _create(c, sess, record)
    if ok:
        await bus.log("      🦋 Bluesky: пост опубликован", "success")
    return ok


async def _pds_host(c: httpx.AsyncClient, did: str) -> str | None:
    """PDS-хост аккаунта из его DID-документа (video-сервис требует токен с
    аудиенсом = did:web:<этот хост>, а не did:web:video.bsky.app)."""
    try:
        from urllib.parse import urlparse
        doc = (await c.get(f"https://plc.directory/{did}")).json()
        for svc in doc.get("service", []):
            if svc.get("type") == "AtprotoPersonalDataServer" or \
                    str(svc.get("id", "")).endswith("atproto_pds"):
                return urlparse(svc["serviceEndpoint"]).hostname
    except Exception:
        pass
    return None


async def _service_auth(c: httpx.AsyncClient, sess: dict, aud: str, lxm: str) -> str | None:
    """Сервисный JWT с нужным аудиенсом и lxm-методом."""
    try:
        r = await c.get(f"{_PDS}/xrpc/com.atproto.server.getServiceAuth",
                        params={"aud": aud, "lxm": lxm, "exp": int(time.time()) + 1800},
                        headers={"Authorization": f"Bearer {sess['jwt']}"})
        r.raise_for_status()
        return r.json()["token"]
    except Exception as e:
        await bus.log(f"      ⚠ Bluesky: сервис-авторизация видео не удалась ({type(e).__name__})", "warn")
        return None


async def post_video(text: str, video_path: Path) -> bool:
    """Видео-пост (ролик Shorts). Правильный флоу: video.bsky.app транскодит mp4,
    ждём завершения job, только потом создаём пост с готовым blob."""
    if not _enabled() or not video_path.exists():
        return False
    async with httpx.AsyncClient(timeout=300) as c:
        sess = await _session(c)
        if not sess:
            return False
        host = await _pds_host(c, sess["did"])
        if not host:
            await bus.log("      ⚠ Bluesky: не резолвится PDS-хост для видео", "warn")
            return False
        # аудиенс = PDS-хост, lxm = com.atproto.repo.uploadBlob (так требует video-сервис)
        token = await _service_auth(c, sess, f"did:web:{host}",
                                    "com.atproto.repo.uploadBlob")
        if not token:
            return False
        # 1) загрузка в видео-сервис → получаем job (с ретраями на 502/таймаут)
        data = video_path.read_bytes()
        job_id, blob, js = None, None, {}
        for attempt in range(3):
            try:
                up = await c.post(f"{_VIDEO_HOST}/xrpc/app.bsky.video.uploadVideo",
                                  params={"did": sess["did"], "name": video_path.name},
                                  headers={"Authorization": f"Bearer {token}",
                                           "Content-Type": "video/mp4"},
                                  content=data)
                if up.status_code in (502, 503, 504):
                    await asyncio.sleep(5)
                    continue
                body = up.json() or {}
                # при 200 поля во вложенном jobStatus; при 409 already_exists —
                # прямо на верхнем уровне ответа. Берём и так, и так.
                js = body.get("jobStatus") or body
                job_id = js.get("jobId")
                blob = js.get("blob")  # иногда уже готов (если грузили раньше)
                break
            except Exception:
                await asyncio.sleep(5)
                continue
        # 2) ждём транскодинг (до ~3 минут)
        for _ in range(60):
            if blob:
                break
            if not job_id:
                await bus.log("      ⚠ Bluesky: видео-сервис не вернул job", "warn")
                return False
            await asyncio.sleep(3)
            try:
                st = await c.get(f"{_VIDEO_HOST}/xrpc/app.bsky.video.getJobStatus",
                                 params={"jobId": job_id},
                                 headers={"Authorization": f"Bearer {token}"})
                jst = (st.json() or {}).get("jobStatus") or {}
                state = jst.get("state")
                if state == "JOB_STATE_COMPLETED":
                    blob = jst.get("blob")
                    break
                if state == "JOB_STATE_FAILED":
                    await bus.log("      ⚠ Bluesky: видео не обработалось (job failed)", "warn")
                    return False
            except Exception:
                continue
        if not blob:
            await bus.log("      ⚠ Bluesky: видео обрабатывалось слишком долго — пропускаю", "warn")
            return False
        # 3) создаём пост с готовым blob
        record = {"$type": "app.bsky.feed.post", "text": text[:300], "createdAt": _now(),
                  "embed": {"$type": "app.bsky.embed.video", "video": blob,
                            "aspectRatio": {"width": 9, "height": 16}}}
        ok = await _create(c, sess, record)
    if ok:
        await bus.log("      🦋 Bluesky: видео опубликовано", "success")
    return ok
