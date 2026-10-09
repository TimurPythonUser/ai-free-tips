"""Точка входа. Запуск локального сервера с дашбордом.

    python run.py

Панель откроется в браузере автоматически: http://127.0.0.1:8000
"""
import threading
import webbrowser

import uvicorn

URL = "http://127.0.0.1:8000"

if __name__ == "__main__":
    # открыть браузер через 2 сек после старта (сервер успеет подняться)
    threading.Timer(2.0, lambda: webbrowser.open(URL)).start()
    print(f"\n>>> Панель: {URL}   (для остановки нажми Ctrl+C)\n")
    uvicorn.run("web.main:app", host="127.0.0.1", port=8000, reload=False)
