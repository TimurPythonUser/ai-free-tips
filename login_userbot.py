"""ОДИН РАЗ: логин твоего Telegram-аккаунта для комментинг-юзербота.
Спросит номер телефона и код из Telegram, выдаст StringSession — строку,
которую надо вписать в .env как USERBOT_SESSION.

Перед запуском в .env должны быть заданы TELEGRAM_API_ID и TELEGRAM_API_HASH
(бери на https://my.telegram.org → API development tools).

Запуск:  python login_userbot.py
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

from app import config

if not (config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH):
    raise SystemExit("Сначала впиши TELEGRAM_API_ID и TELEGRAM_API_HASH в .env "
                     "(получить на https://my.telegram.org).")

print("Заходим в твой аккаунт. Введи номер телефона и код из Telegram.\n")
with TelegramClient(StringSession(), config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH) as client:
    session = client.session.save()
    me = client.get_me()
    print("\n✅ Успешно вошёл как:", me.first_name, f"(@{me.username})" if me.username else "")
    print("\n=== СКОПИРУЙ ЭТУ СТРОКУ В .env как USERBOT_SESSION ===\n")
    print(session)
    print("\n⚠️ Это ключ от твоего аккаунта — никому не показывай и не коммить в git!")
