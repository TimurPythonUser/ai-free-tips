"""Конфигурация: читаем .env и задаём пути."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    """Значение из окружения со срезанным inline-комментарием и пробелами.
    Docker env_file (в отличие от python-dotenv) НЕ срезает '# ...' после значения —
    делаем это сами, чтобы 'FRESH_DAYS=30   # коммент' не ломал парсинг."""
    v = os.getenv(name)
    if v is None:
        return default
    if " #" in v:
        v = v.split(" #", 1)[0]
    return v.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (ValueError, TypeError):
        return default

# --- Каталоги / файлы данных ---
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
HISTORY_FILE = DATA_DIR / "history.json"   # что уже публиковали (дедупликация)
QUEUE_FILE = DATA_DIR / "queue.json"       # очередь отобранных, но ещё не опубликованных
LOG_FILE = DATA_DIR / "agent.log"          # лог всех действий агента

# --- DeepSeek (мозг-редактор + движок креативщика) ---
DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = _env("DEEPSEEK_MODEL", "deepseek-chat")

# --- Роли: температуры и модель креативщика ---
# Редактор (отбор фактов) — низкая температура: нужна дисциплина, а не фантазия.
EDITOR_TEMPERATURE = _env_float("EDITOR_TEMPERATURE", 0.2)
# Креативщик (написание постов) — высокая температура: нужна вариативность.
CREATIVE_TEMPERATURE = _env_float("CREATIVE_TEMPERATURE", 0.85)
# Модель креативщика. По умолчанию тот же DeepSeek; можно указать другую
# (напр. модель, которая живее пишет по-русски) через CREATIVE_MODEL в .env.
CREATIVE_MODEL = _env("CREATIVE_MODEL", DEEPSEEK_MODEL)
# Сколько последних постов показывать креативщику для анти-повтора.
ANTIREPEAT_LOOKBACK = _env_int("ANTIREPEAT_LOOKBACK", 10)

# --- Надёжность HTTP ---
HTTP_RETRIES = _env_int("HTTP_RETRIES", 3)             # попыток на сетевой вызов
HTTP_RETRY_BACKOFF = _env_float("HTTP_RETRY_BACKOFF", 2.0)  # база экспоненты, сек

# --- Perplexity (движок поиска) ---
PERPLEXITY_API_KEY = _env("PERPLEXITY_API_KEY")
PERPLEXITY_BASE_URL = _env("PERPLEXITY_BASE_URL", "https://api.perplexity.ai").rstrip("/")
PERPLEXITY_MODEL = _env("PERPLEXITY_MODEL", "sonar-pro")

# --- Exa (прямой HTTP API поиска, https://api.exa.ai) ---
EXA_API_KEY = _env("EXA_API_KEY")
EXA_BASE_URL = _env("EXA_BASE_URL", "https://api.exa.ai").rstrip("/")
EXA_NUM_RESULTS = _env_int("EXA_NUM_RESULTS", 10)

# --- Tavily (прямой HTTP API поиска, https://api.tavily.com) ---
TAVILY_API_KEY = _env("TAVILY_API_KEY")
TAVILY_BASE_URL = _env("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/")
TAVILY_MAX_RESULTS = max(0, min(20, _env_int("TAVILY_MAX_RESULTS", 10)))
TAVILY_SEARCH_DEPTH = _env("TAVILY_SEARCH_DEPTH", "advanced")
# topic=news даёт свежесть и даты публикации у результатов
TAVILY_TOPIC = _env("TAVILY_TOPIC", "news")
TAVILY_TIME_RANGE = _env("TAVILY_TIME_RANGE", "month")  # day|week|month|year

# --- Ресёрчер: ленивый Perplexity (вариант А) ---
# Сначала жарят Tavily+Exa. Если они дали >= PERPLEXITY_FALLBACK_MIN_FRESH
# уникальных источников со свежей датой (в пределах окна) — Perplexity НЕ зовём.
FRESH_SOURCE_WINDOW_DAYS = _env_int("FRESH_SOURCE_WINDOW_DAYS", 21)
PERPLEXITY_FALLBACK_MIN_FRESH = _env_int("PERPLEXITY_FALLBACK_MIN_FRESH", 12)

# --- Telegram ---
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL = _env("TELEGRAM_CHANNEL", "@ai_free_tips")
# Личка владельца (числовой chat_id): туда бот шлёт готовый ролик с подписями
# для ручной выкладки в TikTok/Shorts. Узнать свой id: напиши боту @userinfobot.
OWNER_CHAT_ID = _env("OWNER_CHAT_ID")

# --- Видео-конвейер (faceless-ролики) ---
VIDEO_DIR = DATA_DIR / "videos"          # готовые mp4
STOCK_DIR = DATA_DIR / "stock"           # кэш стоковых клипов Pexels
MUSIC_DIR = DATA_DIR / "music"           # royalty-free треки для фона (рандом на ролик)
VIDEO_HISTORY_FILE = DATA_DIR / "video_history.json"  # про что ролики уже были
VIDEO_DIR.mkdir(exist_ok=True)
STOCK_DIR.mkdir(exist_ok=True)
MUSIC_DIR.mkdir(exist_ok=True)
# Pexels (https://www.pexels.com/api/) — бесплатный сток. Пусто = градиентный фон.
PEXELS_API_KEY = _env("PEXELS_API_KEY")
# Голос озвучки edge-tts (посмотреть все: edge-tts --list-voices | findstr ru-RU)
VIDEO_VOICE = _env("VIDEO_VOICE", "ru-RU-DmitryNeural")
# Английский голос — для отдельного ролика под Bluesky (площадка англоязычная)
VIDEO_VOICE_EN = _env("VIDEO_VOICE_EN", "en-US-AndrewNeural")
# Темп речи: "+0%" — обычный, "+10%" — чуть бодрее (для коротких роликов норм)
VIDEO_RATE = _env("VIDEO_RATE", "+10%")
# Температура сценариста — как у креативщика, нужна живость
VIDEO_SCRIPT_TEMPERATURE = _env_float("VIDEO_SCRIPT_TEMPERATURE", 0.8)
# Постить ли ролик в канал. false = тест-режим: ролик только в файл и в личку
VIDEO_POST_TO_CHANNEL = _env("VIDEO_POST_TO_CHANNEL", "true").lower() in ("1", "true", "yes")
# Размер кадра ШxВ. На слабом сервере (1 ГБ RAM) ставь 720x1280 — вдвое легче для FFmpeg.
try:
    VIDEO_W, VIDEO_H = (int(x) for x in _env("VIDEO_SIZE", "1080x1920").lower().split("x"))
except Exception:
    VIDEO_W, VIDEO_H = 1080, 1920

# --- Комментинг-помощник ---
# Список каналов-доноров (по одному @username на строку) — редактируй руками.
COMMENT_CHANNELS_FILE = DATA_DIR / "comment_channels.txt"
# Что уже видели (последний пост каждого канала) — для режима черновиков в личку.
COMMENT_STATE_FILE = DATA_DIR / "comment_state.json"
# Не больше стольких черновиков за один прогон (чтобы не заспамить личку).
COMMENT_MAX_PER_RUN = _env_int("COMMENT_MAX_PER_RUN", 3)
# Температура генерации комментов — высокая для живости и разнообразия (DeepSeek: 0-2).
COMMENT_TEMPERATURE = _env_float("COMMENT_TEMPERATURE", 1.5)
# Автопроверка раз в N минут (для worker.py на сервере). 0 = выключено.
COMMENT_CHECK_MINUTES = _env_int("COMMENT_CHECK_MINUTES", 0)

# --- Юзербот (автомат с подтверждением: постит коммент ОТ ИМЕНИ КАНАЛА) ---
# API-креды с https://my.telegram.org → API development tools. ОБЯЗАТЕЛЬНЫ для юзербота.
TELEGRAM_API_ID = _env_int("TELEGRAM_API_ID", 0)
TELEGRAM_API_HASH = _env("TELEGRAM_API_HASH")
# StringSession твоего аккаунта (получается один раз через login_userbot.py). НЕ ДЕЛИСЬ ИМ.
USERBOT_SESSION = _env("USERBOT_SESSION")
# Файл счётчиков лимитов юзербота (сколько комментов за день, по каналам).
USERBOT_STATE_FILE = DATA_DIR / "userbot_state.json"
# Лимиты «чтоб не спалиться» (см. рекомендации):
USERBOT_DAILY_MAX = _env_int("USERBOT_DAILY_MAX", 10)          # всего комментов в сутки
USERBOT_PER_CHANNEL_DAILY = _env_int("USERBOT_PER_CHANNEL_DAILY", 1)  # на один канал в сутки
USERBOT_MIN_DELAY_SEC = _env_int("USERBOT_MIN_DELAY_SEC", 120)  # мин. пауза перед постингом
USERBOT_MAX_DELAY_SEC = _env_int("USERBOT_MAX_DELAY_SEC", 600)  # макс. пауза (рандом между ними)
# Память: какие посты уже предлагали (чтобы не дублировать). Авто-обрезка до MAX записей.
USERBOT_SEEN_FILE = DATA_DIR / "userbot_seen.json"
USERBOT_SEEN_MAX = _env_int("USERBOT_SEEN_MAX", 500)
# Журнал событий комментинга (предложено/запощено/ошибки) — для суточного отчёта.
USERBOT_STATS_FILE = DATA_DIR / "userbot_stats.json"
# Именные пригласительные ссылки-счётчики ({"tiktok": url, "shorts": url}).
# Юзербот создаёт их сам при старте; конвейер подставляет в подписи роликов.
INVITE_LINKS_FILE = DATA_DIR / "invite_links.json"
# Вчерашний снапшот статистики ссылок/подписчиков — для дельт в отчёте.
INVITE_STATS_FILE = DATA_DIR / "invite_stats.json"
# Суточные лимиты запросов к публичному боту-поисковику халявы.
SEARCHBOT_LIMITS_FILE = DATA_DIR / "searchbot_limits.json"
# Bluesky (AT Protocol) — автопостинг постов и роликов. Пусто = выключено.
BLUESKY_HANDLE = _env("BLUESKY_HANDLE", "")
BLUESKY_APP_PASSWORD = _env("BLUESKY_APP_PASSWORD", "")
# Во сколько слать суточный отчёт по комментингу (HH:MM, пусто = выкл).
COMMENT_REPORT_TIME = _env("COMMENT_REPORT_TIME", "22:30")
# Фолбэк по простою: если N минут не было НИ ОДНОГО нового поста в донорах —
# сам предложить свежий ещё не откомментированный пост (ротация по каналам). 0 = выкл.
USERBOT_IDLE_MINUTES = _env_int("USERBOT_IDLE_MINUTES", 120)
# Режим «вечерний разбор»: раз в день собрать пачку черновиков со всех каналов
# и прислать в личку — вечером спокойно всё поапрувишь. HH:MM, пусто = выкл.
COMMENT_DIGEST_TIME = _env("COMMENT_DIGEST_TIME", "")
# Сколько черновиков максимум в вечерней пачке (по одному с канала → до 20 на ревью).
COMMENT_DIGEST_MAX = _env_int("COMMENT_DIGEST_MAX", 20)
# Реагировать ли на посты в реальном времени (днём). При вечернем разборе обычно выкл.
USERBOT_REALTIME = _env("USERBOT_REALTIME", "true").lower() in ("1", "true", "yes")
# Прогнать вечерний разбор сразу при старте (разово, чтоб получить пачку сейчас).
USERBOT_DIGEST_ON_START = _env("USERBOT_DIGEST_ON_START", "false").lower() in ("1", "true", "yes")
# Включён ли КОММЕНТИНГ (мониторинг каналов + пачки + постинг от аккаунта).
# false = комментинг выкл (после страйка), но бот-поисковик и команды /report,
# /scout продолжают работать — они в том же процессе, но безопасны (только bot-клиент).
USERBOT_COMMENTING = _env("USERBOT_COMMENTING", "true").lower() in ("1", "true", "yes")

# --- Параметры отбора ---
FRESH_DAYS = _env_int("FRESH_DAYS", 7)
DEDUP_DAYS = _env_int("DEDUP_DAYS", 30)
POST_COUNT = _env_int("POST_COUNT", 5)   # сколько добывать за один прогон

# --- Автономное расписание (worker.py) ---
# Таймзона для всех слотов расписания (сервер может быть в UTC).
TIMEZONE = _env("TIMEZONE", "Europe/Kyiv")
# Слоты добычи через запятую (HH:MM,HH:MM). Несколько раз в день = больше шансов
# поймать свежие халявы (реально новых 1-2 в день, а публикуем 2 — иначе очередь пустеет).
HARVEST_TIMES = [t.strip() for t in _env("HARVEST_TIME", "07:00,14:00").split(",") if t.strip()]
HARVEST_TIME = HARVEST_TIMES[0] if HARVEST_TIMES else "07:00"  # совместимость
# Слоты публикации через запятую. Их количество = постов в день.
PUBLISH_TIMES = [t.strip() for t in _env("PUBLISH_TIMES", "10:00,19:00").split(",") if t.strip()]
# Слоты роликов через запятую (HH:MM,HH:MM). Их количество = роликов в день.
# Пусто = видео-конвейер выключен. Темы не дублируются: каждый слот берёт свежую
# халяву, которой ещё не было в video_history.json.
VIDEO_TIMES = [t.strip() for t in _env("VIDEO_TIME", "").split(",") if t.strip()]
# Обратная совместимость: первый слот как одиночное значение (старый код/логи).
VIDEO_TIME = VIDEO_TIMES[0] if VIDEO_TIMES else ""
# В какие дни недели искать гранты для ИИ-стартапов дополнительным потоком
# (ISO: 1=пн … 7=вс). Тема мелькает регулярно, но реже основных халяв.
GRANTS_WEEKDAYS = {int(d) for d in _env("GRANTS_WEEKDAYS", "1,4").split(",") if d.strip().isdigit()}
# Сколько дней халява живёт в очереди, прежде чем протухнет и будет выкинута.
QUEUE_TTL_DAYS = _env_int("QUEUE_TTL_DAYS", 4)
# Держать в очереди не больше стольких халяв (буфер на пару дней).
QUEUE_MAX = _env_int("QUEUE_MAX", 12)


def _parse_hhmm(value: str) -> tuple[int, int]:
    """'19:30' -> (19, 30). При ошибке -> (0, 0)."""
    try:
        h, m = value.split(":")
        return int(h), int(m)
    except Exception:
        return 0, 0


def missing_keys() -> list[str]:
    """Список незаполненных обязательных ключей — для дружелюбной ошибки в UI.
    Обязательны: DeepSeek (редактор+креативщик), Telegram, и ХОТЯ БЫ ОДИН движок
    поиска (Tavily / Exa / Perplexity)."""
    miss = []
    if not DEEPSEEK_API_KEY:
        miss.append("DEEPSEEK_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        miss.append("TELEGRAM_BOT_TOKEN")
    if not (TAVILY_API_KEY or EXA_API_KEY or PERPLEXITY_API_KEY):
        miss.append("любой из: TAVILY_API_KEY / EXA_API_KEY / PERPLEXITY_API_KEY")
    return miss
