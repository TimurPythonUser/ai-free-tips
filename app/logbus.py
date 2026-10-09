"""Шина логов: транслирует каждое действие агента в веб-интерфейс (SSE),
хранит недавнюю историю и пишет в файл."""
import asyncio
import json
import sys
from datetime import datetime

from . import config

# Консоль Windows часто в cp1251 и падает на эмодзи — переводим stdout в UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class LogBus:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self.recent: list[dict] = []
        self._status: dict = {"running": False, "last_run": None, "last_result": None}

    # --- подписки веб-клиентов ---
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    # --- запись строки лога ---
    async def log(self, message: str, level: str = "info", **extra) -> None:
        entry = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
        if extra:
            entry["extra"] = extra

        self.recent.append(entry)
        self.recent = self.recent[-500:]

        try:
            print(f"[{entry['ts']}] {level.upper()}: {message}", flush=True)
        except Exception:
            pass
        try:
            with open(config.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

        for q in list(self._subscribers):
            try:
                q.put_nowait(entry)
            except Exception:
                pass

    # --- статус выполнения ---
    def set_status(self, **kw) -> None:
        self._status.update(kw)

    @property
    def status(self) -> dict:
        return self._status


bus = LogBus()
