import asyncio
import os
from contextlib import suppress

import uvicorn
from fastapi import FastAPI

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

from telethon import TelegramClient
from telethon.sessions import StringSession


# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

PORT = int(os.getenv("PORT", "10000"))


# =========================
# CHECK CONFIG
# =========================

print("========== SENTINEL-X ==========")
print("BOT_TOKEN:", "OK" if BOT_TOKEN else "MISSING")
print("ADMIN_ID:", ADMIN_ID)
print("API_ID:", API_ID)
print("API_HASH:", "OK" if API_HASH else "MISSING")
print("SESSION_STRING:", "OK" if SESSION_STRING else "MISSING")
print("PORT:", PORT)
print("================================")


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing")


# =========================
# TELEGRAM BOT
# =========================

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# =========================
# TELETHON ACCOUNT
# =========================

user_client = None

if API_ID and API_HASH and SESSION_STRING:
    user_client = TelegramClient(
        StringSession(SESSION_STRING),
        API_ID,
        API_HASH
    )


# =========================
# WEB SERVER
# =========================

app = FastAPI()


@app.get("/")
async def root():
    return {
        "service": "SENTINEL-X",
        "status": "online"
    }


@app.get("/health")
async def health():
    return {
        "status": "ok"
    }


# =========================
# BOT COMMAND
# =========================

@dp.message(CommandStart())
async def start_command(message: Message):

    print(
        f"/start from "
        f"{message.from_user.id if message.from_user else 'unknown'}"
    )

    if message.from_user is None:
        return

    if message.from_user.id != ADMIN_ID:

        await message.answer(
            "⛔ Доступ запрещён.\n\n"
            f"Твой Telegram ID: <code>{message.from_user.id}</code>\n"
            f"ADMIN_ID: <code>{ADMIN_ID}</code>"
        )

        return

    await message.answer(
        "🛡 <b>SENTINEL-X</b>\n\n"
        "Бот успешно работает.\n\n"
        "🟢 Bot API: ONLINE\n"
        f"👤 ADMIN ID: <code>{ADMIN_ID}</code>\n"
        f"🔐 Telethon: "
        f"<b>{'CONNECTED' if user_client else 'NOT CONFIGURED'}</b>\n\n"
        "Автоматическая защита будет подключена следующим этапом."
    )


# =========================
# TELETHON CONNECTION
# =========================

async def connect_user_account():

    if user_client is None:

        print(
            "⚠️ Telethon не запущен: "
            "API_ID/API_HASH/SESSION_STRING не заданы."
        )

        return

    try:

        print("🔐 Подключаю Telegram account...")

        await user_client.connect()

        if not await user_client.is_user_authorized():

            print(
                "❌ SESSION_STRING недействителен "
                "или авторизация отсутствует."
            )

            return

        me = await user_client.get_me()

        print(
            "✅ Telegram account connected:"
        )

        print(
            f"   ID: {me.id}"
        )

        print(
            f"   Name: "
            f"{me.first_name or ''} "
            f"{me.last_name or ''}"
        )

    except Exception as e:

        print(
            "❌ Telethon error:",
            type(e).__name__,
            str(e)
        )


# =========================
# WEB SERVER
# =========================

async def run_web():

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info"
    )

    server = uvicorn.Server(config)

    await server.serve()


# =========================
# BOT POLLING
# =========================

async def run_bot():

    print("🤖 Запускаю Telegram Bot polling...")

    me = await bot.get_me()

    print(
        f"✅ Bot connected: "
        f"@{me.username}"
    )

    print(
        "🟢 SENTINEL-X ГОТОВ."
    )

    await dp.start_polling(bot)


# =========================
# MAIN
# =========================

async def main():

    print("🚀 Starting SENTINEL-X...")

    # Подключаем пользовательский Telegram-аккаунт
    # отдельно от Bot API.
    await connect_user_account()

    # Запускаем Web + Bot одновременно.
    await asyncio.gather(
        run_bot(),
        run_web()
    )


if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print("Stopped.")

    finally:

        with suppress(Exception):
            asyncio.run(bot.session.close())

        if user_client:

            with suppress(Exception):
                asyncio.run(user_client.disconnect())