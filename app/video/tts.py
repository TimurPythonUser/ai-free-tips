"""Озвучка через edge-tts (бесплатные нейроголоса Microsoft).
Возвращает mp3 + тайминги каждого слова — из них строятся синхронные субтитры."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import edge_tts

from .. import config
from ..logbus import bus

# Голоса Microsoft периодически «флапают»: та же самая команда через раз
# отдаёт NoAudioReceived (пустой ответ) вместо аудио. Бывает и хуже: сервис
# часами не отдаёт звук для конкретной связки голос+скорость (27.09.2026:
# DmitryNeural с rate=+10% падал стабильно, а он же с +0% и SvetlanaNeural
# с +10% — работали). Поэтому повторы одной и той же связки не спасают —
# после них перебираем варианты: без ускорения, затем запасной голос.
_ATTEMPTS_MAIN = 3       # основной голос с VIDEO_RATE
_ATTEMPTS_FALLBACK = 2   # каждый запасной вариант

# Запасные голоса по локали (первые 5 символов имени голоса).
_BACKUP_VOICES = {
    "ru-RU": ["ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"],
    "en-US": ["en-US-AriaNeural", "en-US-AndrewNeural"],
}


@dataclass
class Word:
    text: str
    start: float  # секунды от начала
    end: float


def _variants(voice: str) -> list[tuple[str, str]]:
    """Очередь попыток (голос, скорость): основная связка → без ускорения → запасной голос."""
    rate = config.VIDEO_RATE
    out = [(voice, rate)] * _ATTEMPTS_MAIN
    if rate.strip().lstrip("+") not in ("0%", ""):
        out += [(voice, "+0%")] * _ATTEMPTS_FALLBACK
    for v in _BACKUP_VOICES.get(voice[:5], []):
        if v != voice:
            out += [(v, rate)] * _ATTEMPTS_FALLBACK
            break
    return out


async def _synth_once(text: str, voice: str, rate: str) -> tuple[bytes, list[Word]]:
    audio = bytearray()
    words: list[Word] = []
    # boundary="WordBoundary": с 7-й версии edge-tts по умолчанию отдаёт
    # границы предложений, а для синхронных субтитров нужны границы слов
    communicate = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            start = chunk["offset"] / 10_000_000
            dur = chunk["duration"] / 10_000_000
            words.append(Word(text=chunk["text"], start=start, end=start + dur))
    return bytes(audio), words


async def synth(text: str, out_mp3: Path, voice: str | None = None) -> list[Word]:
    """Озвучивает текст в out_mp3, возвращает список слов с таймингами.
    edge-tts отдаёт WordBoundary-события в 100-наносекундных тиках.
    voice=None → русский голос по умолчанию; для англ. ролика (Bluesky)
    передаём config.VIDEO_VOICE_EN.
    Устойчиво к флапу edge-tts (NoAudioReceived): перебирает варианты из
    _variants(), аудио пишет в файл только при успехе, чтобы не оставить битый mp3."""
    voice = voice or config.VIDEO_VOICE
    variants = _variants(voice)
    last_err = "неизвестно"
    for attempt, (v, rate) in enumerate(variants, 1):
        if attempt > 1 and (v, rate) != variants[attempt - 2]:
            await bus.log(f"      🔀 переключаюсь на запасной вариант: {v}, скорость {rate}", "warn")
        try:
            audio, words = await _synth_once(text, v, rate)
            if audio and words:
                out_mp3.write_bytes(audio)
                if attempt > 1:
                    await bus.log(f"      🔁 озвучка удалась с {attempt}-й попытки ({v}, {rate})",
                                  "success")
                return words
            last_err = "пустой ответ (нет аудио/слов)"
        except Exception as e:  # NoAudioReceived и прочие сетевые сбои edge-tts
            last_err = type(e).__name__
        if attempt < len(variants):
            await bus.log(f"      🔁 edge-tts осёкся ({last_err}) — повтор "
                          f"{attempt + 1}/{len(variants)}…", "warn")
            await asyncio.sleep(2 * min(attempt, 4))
    raise RuntimeError(f"edge-tts не отдал озвучку за {len(variants)} попыток: {last_err}")


def total_duration(words: list[Word], tail: float = 0.7) -> float:
    """Длительность ролика: конец последнего слова + небольшой хвост тишины."""
    return words[-1].end + tail
