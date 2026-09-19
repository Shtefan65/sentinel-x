import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    AuthKeyUnregisteredError,
    SessionRevokedError,
)


API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")


async def main():
    print("=" * 60)
    print("SENTINEL-X SESSION TEST")
    print("=" * 60)

    if not API_ID:
        print("❌ API_ID не задан")
        return

    if not API_HASH:
        print("❌ API_HASH не задан")
        return

    if not SESSION_STRING:
        print("❌ SESSION_STRING не задан")
        return

    print("API_ID: OK")
    print("API_HASH: OK")
    print(f"SESSION_STRING: {len(SESSION_STRING)} символов")
    print()
    print("Подключаемся к Telegram...")
    print()

    client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH,
    )

    try:
        await client.connect()

        if not await client.is_user_authorized():
            print("❌ SESSION_STRING НЕ АВТОРИЗОВАН")
            print()
            print("Telegram не считает эту сессию действующей.")
            return

        me = await client.get_me()

        print("✅ SESSION_STRING РАБОТАЕТ")
        print()
        print(f"Имя: {me.first_name or '-'}")
        print(f"Фамилия: {me.last_name or '-'}")
        print(f"Username: @{me.username}" if me.username else "Username: -")
        print(f"ID: {me.id}")
        print()
        print("Telegram-сессия действительна.")
        print("SENTINEL-X сможет использовать её.")

    except SessionRevokedError:
        print("❌ SESSION_REVOKED")
        print()
        print("Эта Telegram-сессия была отозвана.")

    except AuthKeyUnregisteredError:
        print("❌ AUTH_KEY_UNREGISTERED")
        print()
        print("Ключ авторизации этой сессии больше не зарегистрирован.")

    except Exception as e:
        print("❌ ОШИБКА")
        print()
        print(f"Тип: {type(e).__name__}")
        print(f"Сообщение: {e}")

    finally:
        await client.disconnect()

    print()
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())