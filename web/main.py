"""FastAPI: дашборд с живыми логами (SSE) и кнопкой «Обновить»."""
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from app import agent, config, store
from app.logbus import bus
from app.video import pipeline as video_pipeline

app = FastAPI(title="AI Free Tips — агент")
STATIC = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.post("/api/refresh")
async def refresh():
    if bus.status.get("running"):
        return JSONResponse({"ok": False, "message": "Уже выполняется"})
    missing = config.missing_keys()
    if missing:
        return JSONResponse(
            {"ok": False, "message": "Не заполнены ключи: " + ", ".join(missing)}
        )
    asyncio.create_task(agent.run())
    return JSONResponse({"ok": True, "message": "Поиск запущен"})


@app.post("/api/make_video")
async def make_video():
    if bus.status.get("running"):
        return JSONResponse({"ok": False, "message": "Уже выполняется"})
    missing = config.missing_keys()
    if missing:
        return JSONResponse(
            {"ok": False, "message": "Не заполнены ключи: " + ", ".join(missing)}
        )
    asyncio.create_task(video_pipeline.run())
    return JSONResponse({"ok": True, "message": "Делаю ролик"})


@app.get("/api/status")
async def status():
    return {
        "status": bus.status,
        "channel": config.TELEGRAM_CHANNEL,
        "missing_keys": config.missing_keys(),
    }


@app.get("/api/history")
async def history():
    return list(reversed(store.load()))[:20]


@app.get("/api/stream")
async def stream(request: Request):
    q = bus.subscribe()

    async def gen():
        try:
            for e in bus.recent[-100:]:
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    e = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")
