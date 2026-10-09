"""Автономный воркер для сервера: крутится вечно, работает по расписанию,
БЕЗ веб-панели.

Расписание (время — из .env, по таймзоне TIMEZONE):
  • HARVEST_TIME  — раз в день добываем халявы в очередь;
  • PUBLISH_TIMES — в каждый слот публикуем ОДИН пост из очереди.

Запуск на сервере:  python worker.py
"""
from __future__ import annotations

import asyncio

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import agent, commenting, config
from app.logbus import bus
from app.video import pipeline as video_pipeline


def _tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(config.TIMEZONE)
        except Exception:
            pass
    return None  # APScheduler возьмёт локальную таймзону


async def _job_harvest():
    await bus.log("⏰ [Расписание] слот ДОБЫЧИ — запускаю harvest()")
    await agent.harvest()


async def _job_publish():
    await bus.log("⏰ [Расписание] слот ПУБЛИКАЦИИ — запускаю publish_one()")
    await agent.publish_one()


async def _job_video():
    await bus.log("⏰ [Расписание] слот РОЛИКА — запускаю видео-конвейер")
    await video_pipeline.run()


async def main():
    missing = config.missing_keys()
    if missing:
        await bus.log(f"❌ Не заполнены ключи в .env: {', '.join(missing)}. Воркер не стартует.", "error")
        return

    tz = _tz()
    scheduler = AsyncIOScheduler(timezone=tz) if tz else AsyncIOScheduler()

    # --- слоты добычи (несколько раз в день, чтобы очередь не пустела) ---
    for i, slot in enumerate(config.HARVEST_TIMES):
        hh, mm = config._parse_hhmm(slot)
        scheduler.add_job(_job_harvest, CronTrigger(hour=hh, minute=mm, timezone=tz),
                          id=f"harvest_{i}", misfire_grace_time=3600, coalesce=True)

    # --- слоты публикации ---
    for i, slot in enumerate(config.PUBLISH_TIMES):
        h, m = config._parse_hhmm(slot)
        scheduler.add_job(_job_publish, CronTrigger(hour=h, minute=m, timezone=tz),
                          id=f"publish_{i}", misfire_grace_time=3600, coalesce=True)

    # --- слоты роликов (VIDEO_TIMES пустой = выключено) ---
    # Темы между слотами не дублируются: _pick_topic() пропускает то, что уже в истории.
    for i, slot in enumerate(config.VIDEO_TIMES):
        h, m = config._parse_hhmm(slot)
        scheduler.add_job(_job_video, CronTrigger(hour=h, minute=m, timezone=tz),
                          id=f"video_{i}", misfire_grace_time=3600, coalesce=True)

    # --- комментинг-помощник (каждые N минут; 0 = выключено) ---
    if config.COMMENT_CHECK_MINUTES > 0:
        scheduler.add_job(commenting.check_once, "interval",
                          minutes=config.COMMENT_CHECK_MINUTES,
                          id="comments", misfire_grace_time=600, coalesce=True)

    scheduler.start()

    await bus.log("🤖 Воркер запущен. Канал: " + config.TELEGRAM_CHANNEL)
    await bus.log(f"   🌾 Добыча: {', '.join(config.HARVEST_TIMES)}  |  📣 Публикация: {', '.join(config.PUBLISH_TIMES)}"
                  f"  |  🎬 Ролики: {', '.join(config.VIDEO_TIMES) or 'выкл'}  ({config.TIMEZONE})")
    await bus.log(f"   📦 В очереди сейчас: {__import__('app.store', fromlist=['x']).queue_len()}")
    for job in scheduler.get_jobs():
        await bus.log(f"   ⏭ следующий запуск [{job.id}]: {job.next_run_time}")

    # держим event loop живым вечно
    stop = asyncio.Event()
    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        scheduler.shutdown(wait=False)
        await bus.log("🛑 Воркер остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
