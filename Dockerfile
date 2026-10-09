# syntax=docker/dockerfile:1
# Продакшн-образ бота. Запускает автономный worker.py (без веб-панели).
FROM python:3.12-slim

# PYTHONUNBUFFERED — логи сразу видны в `docker logs` (не буферятся).
# PYTHONDONTWRITEBYTECODE — не мусорим .pyc внутри контейнера.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Шрифты для видео-конвейера: libass (субтитры в ffmpeg) без них не отрендерит текст.
# fonts-liberation — метрический аналог Arial, fontconfig сам подставит его вместо Arial.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fontconfig fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Сначала зависимости отдельным слоем — кэшируются, пока requirements.txt не тронут.
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

# Код — последним слоем (меняется чаще всего).
COPY . .

# Каталог данных (очередь/история/лог). На сервере поверх монтируется volume.
RUN mkdir -p data

# Автономный режим по расписанию, без панели.
CMD ["python", "worker.py"]
