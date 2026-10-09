"""Оркестратор видео-конвейера:
тема (из истории постов) → сценарий → озвучка → субтитры → сток → сборка → публикация.
Каждый шаг логируется в шину, статус разделяется с основным агентом (не работаем параллельно)."""
from __future__ import annotations

import json
import random
import re
import shutil
from datetime import datetime

from .. import bluesky, config, notify, store
from ..logbus import bus
from . import assemble, publish, script as script_mod, stock, subs, tts


def _pick_music():
    """Случайный royalty-free трек из data/music/ (или None — тогда ролик без музыки)."""
    if not config.MUSIC_DIR.exists():
        return None
    tracks = sorted(config.MUSIC_DIR.glob("*.mp3"))
    return random.choice(tracks) if tracks else None


# ---------- история роликов (дедуп тем) ----------

def _history_load() -> list[dict]:
    if config.VIDEO_HISTORY_FILE.exists():
        try:
            return json.loads(config.VIDEO_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _history_add(item: dict, file_name: str) -> None:
    items = _history_load()
    items.append({
        "url_norm": store.norm_url(item.get("url", "")),
        "title": item.get("title", ""),
        "file": file_name,
        "created_at": datetime.now().isoformat(),
    })
    config.VIDEO_HISTORY_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _video_filename(scr: dict) -> str:
    """Имя файла = готовый заголовок ролика: YouTube при загрузке подставляет имя
    файла в поле «Название», так что правильное имя = ноль ручной работы.
    Берём подпись для Shorts без хэштегов, фолбэк — хук."""
    caption = (scr.get("captions") or {}).get("shorts") or ""
    title = caption.split("#")[0].strip() or (scr.get("hook") or "").strip() or "ролик"
    title = re.sub(r'[<>:"/\\|?*\n\r]+', " ", title)      # запрещённые в именах файлов
    title = re.sub(r"\s+", " ", title).strip(" .")[:80]
    return f"{title}.mp4"


def _cleanup(keep_videos: int = 15, stock_limit_mb: int = 400) -> None:
    """Гигиена диска (сервер маленький): сносим рабочие папки сборки,
    держим только последние N роликов и ограничиваем кэш стока по размеру."""
    for d in config.VIDEO_DIR.glob("build_*"):
        shutil.rmtree(d, ignore_errors=True)
    vids = sorted(config.VIDEO_DIR.glob("*.mp4"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for p in vids[keep_videos:]:
        try:
            p.unlink()
        except OSError:
            pass
    total = 0
    for p in sorted(config.STOCK_DIR.glob("*.mp4"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        total += p.stat().st_size
        if total > stock_limit_mb * 1_000_000:
            try:
                p.unlink()
            except OSError:
                pass


def _pick_topic() -> tuple[dict | None, int]:
    """Самая СВЕЖАЯ опубликованная халява, про которую ролика ещё не было.
    Возвращает (тема, сколько_ещё_доступно) — второй ролик за день гарантированно
    возьмёт ДРУГУЮ тему, т.к. первый уже записал свою в историю роликов.
    Сортируем по дате публикации явно (не по позиции в файле). Фолбэк — очередь."""
    done = {it.get("url_norm") for it in _history_load()}

    def _freshness(it: dict) -> datetime:
        """Свежесть: сначала дата самой новости, нет её — дата поста в канале."""
        for field in ("date", "posted_at"):
            v = it.get(field)
            if v:
                try:
                    return datetime.fromisoformat(str(v)[:19])
                except Exception:
                    continue
        return datetime.min

    uncovered = [it for it in store.load()
                 if it.get("url_norm") and it["url_norm"] not in done]
    uncovered.sort(key=_freshness, reverse=True)  # самое свежее событие — первым
    if uncovered:
        return uncovered[0], len(uncovered)

    # запас: ещё не опубликованные халявы из очереди (не забираем у постера голову)
    queued = [it for it in store.load_queue()
              if store.norm_url(it.get("url", "")) not in done]
    if queued:
        return queued[0], len(queued)
    return None, 0


def _pick_style() -> str:
    """Формат ролика по слоту (время контейнера — UTC; 11:00 Киев=08:00, 16:00=13:00):
    утро — классика, день — «разбор за 3 шага», воскресный день — «топ-3 недели».
    Разные форматы = больше шансов, что какой-то зайдёт алгоритму."""
    now = datetime.now()
    if now.isoweekday() == 7 and now.hour >= 12:
        return "top3"
    return "classic" if now.hour < 12 else "howto"


def _top3_items() -> list[dict]:
    """Три свежайшие опубликованные халявы для воскресного дайджеста.
    Темы НЕ помечаются в истории роликов: дайджест — повтор-напоминание, не «расход» тем."""
    out = [it for it in reversed(store.load()) if (it.get("title") or "").strip()]
    return out[:3]


# ---------- английский ролик для Bluesky ----------

async def _bluesky_english_video(scr: dict, clips, music, workdir) -> None:
    """Собирает отдельный АНГЛИЙСКИЙ ролик и постит его в Bluesky.
    Русский ролик (ТГ/YouTube) не трогаем — здесь своя озвучка/субтитры/сборка,
    фон-клипы и музыка переиспользуются. Файл — во временной папке сборки
    (снесётся штатной _cleanup), в YouTube-выгрузку и историю не попадает."""
    await bus.log("   🦋 [Bluesky] Собираю английскую версию ролика…")

    # 1) перевод текста озвучки целиком (не подпись — весь сценарий)
    narration_en = await bluesky.translate_long(script_mod.narration_text(scr))
    if not narration_en.strip():
        await bus.log("      ⚠ Bluesky: пустой перевод озвучки — пропускаю англ. ролик", "warn")
        return

    # 2) английская озвучка + свои тайминги слов
    voice_en = workdir / "voice_en.mp3"
    words_en = await tts.synth(narration_en, voice_en, voice=config.VIDEO_VOICE_EN)
    duration_en = tts.total_duration(words_en)

    # 3) английские субтитры + английская CTA-плашка
    subs_en = workdir / "subs_en.ass"
    subs.write_ass(words_en, duration_en, subs_en,
                   footer=config.TELEGRAM_CHANNEL, cta_text="FULL LIST IN TELEGRAM")

    # 4) сборка (те же клипы и музыка)
    out_en = workdir / "bluesky_en.mp4"
    await assemble.build(clips, voice_en, subs_en, duration_en, out_en, music=music)
    await bus.log(f"      ✅ англ. ролик готов ({out_en.stat().st_size / 1e6:.1f} МБ)")

    # 5) публикация в Bluesky (подпись — англ. хук + ссылка-счётчик)
    hook_en = await bluesky.translate((scr.get("hook") or "").strip())
    link = bluesky.invite_link()
    await bluesky.post_video(f"{hook_en}\n\n🎁 {link}", out_en)


# ---------- сам прогон ----------

async def run() -> None:
    if bus.status.get("running"):
        await bus.log("Уже идёт другой прогон — подожди", "warn")
        return
    bus.set_status(running=True, last_run=datetime.now().strftime("%d.%m %H:%M"))
    try:
        await _run_inner()
        bus.set_status(last_result="ok")
    except Exception as e:
        await bus.log(f"❌ Конвейер упал: {type(e).__name__}: {e}", "error")
        await notify.owner(f"⚠️ ХАЛЯВА AI: видео-конвейер упал ({type(e).__name__}). "
                           "Ролика сегодня не будет — глянь логи.")
        bus.set_status(last_result="error")
    finally:
        bus.set_status(running=False)


async def _run_inner() -> None:
    style = _pick_style()
    style_name = {"classic": "классика", "howto": "разбор за 3 шага",
                  "top3": "топ-3 недели"}.get(style, style)
    await bus.log(f"🎬 Запускаю видео-конвейер… (формат: {style_name})")

    if style == "top3":
        items = _top3_items()
        if len(items) < 3:  # мало истории — откатываемся на классику
            style = "classic"
        else:
            item = items[0]  # для истории/файла ориентируемся на свежайшую
            for it in items:
                await bus.log(f"   📌 В подборке: {it.get('title', '(без названия)')}")

    if style != "top3":
        item, available = _pick_topic()
        if not item:
            await bus.log("❌ Не из чего делать ролик: сначала нажми «Обновить», "
                          "чтобы агент нашёл и опубликовал халявы", "error")
            return
        await bus.log(f"   📌 Тема: {item.get('title', '(без названия)')}  "
                      f"(свежих тем без видео: {available})")

    await bus.log("   ✍️ [1/5] Сценарист пишет сценарий…")
    if style == "top3":
        scr = await script_mod.write_top3(items)
    else:
        scr = await script_mod.write_script(item, style=style)
    if not scr:
        await bus.log("❌ Сценарист не справился — попробуй ещё раз", "error")
        return
    await bus.log(f"      Хук: {scr['hook']}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    workdir = config.VIDEO_DIR / f"build_{ts}"
    workdir.mkdir(parents=True, exist_ok=True)

    await bus.log("   🎙 [2/5] Озвучиваю (edge-tts)…")
    narration = script_mod.narration_text(scr)
    # текст озвучки — рядом со сборкой: если конвейер упадёт, папка останется
    # и падение можно будет воспроизвести на том же тексте
    (workdir / "narration.txt").write_text(narration, encoding="utf-8")
    voice_mp3 = workdir / "voice.mp3"
    words = await tts.synth(narration, voice_mp3)
    duration = tts.total_duration(words)
    await bus.log(f"      {len(words)} слов, ~{duration:.0f} сек")

    await bus.log("   💬 [3/5] Собираю субтитры…")
    subs_ass = workdir / "subs.ass"
    subs.write_ass(words, duration, subs_ass, footer=config.TELEGRAM_CHANNEL)

    await bus.log("   🎞 [4/5] Ищу фоновое видео…")
    clips = await stock.fetch_clips(scr.get("keywords") or [])

    await bus.log("   🛠 [5/5] Склеиваю ролик (FFmpeg)…")
    out_mp4 = config.VIDEO_DIR / _video_filename(scr)
    if out_mp4.exists():  # тот же заголовок дважды — не затираем старый файл
        out_mp4 = config.VIDEO_DIR / f"{out_mp4.stem}_{ts}.mp4"
    music = _pick_music()
    if music:
        await bus.log(f"      🎵 фоновый трек: {music.name}")
    await assemble.build(clips, voice_mp3, subs_ass, duration, out_mp4, music=music)
    size_mb = out_mp4.stat().st_size / 1e6
    await bus.log(f"      ✅ Готово: {out_mp4.name} ({size_mb:.1f} МБ)", "success")

    await bus.log("   📤 Публикую…")
    if config.VIDEO_POST_TO_CHANNEL:
        await publish.to_channel(out_mp4, scr, item)
    else:
        await bus.log("      🧪 тест-режим (VIDEO_POST_TO_CHANNEL=false) — в канал не пощу")
    # источник для Threads: у одиночного ролика — ссылка халявы; у топ-3 их три, не шлём
    source_url = None if style == "top3" else (item.get("url") or "").strip() or None
    await publish.to_owner(out_mp4, scr, source_url)

    # Bluesky: отдельный ПОЛНОСТЬЮ английский ролик (площадка англоязычная).
    # Клипы-фон и музыку переиспользуем — заново гоняем только озвучку/субтитры/сборку.
    try:
        if bluesky._enabled():
            await _bluesky_english_video(scr, clips, music, workdir)
    except Exception as e:
        await bus.log(f"      ⚠ Bluesky: англ. ролик не собрался ({type(e).__name__})", "warn")
        # Bluesky не должен ронять конвейер

    if style != "top3":  # дайджест тем не «расходует» — см. _top3_items()
        _history_add(item, out_mp4.name)
    _cleanup()
    await bus.log("🏁 Видео-конвейер закончил", "success")
