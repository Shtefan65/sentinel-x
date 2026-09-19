import asyncio
import os
import time
from collections import deque, defaultdict
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import FastAPI
import uvicorn

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from telethon import TelegramClient, events, functions
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

PORT = int(os.getenv("PORT", "10000"))


# =========================================================
# DEFENSE SETTINGS
# =========================================================

# Количество входящих сообщений за окно
RAID_WINDOW = 10

# Если за RAID_WINDOW секунд приходит столько сообщений
# от разных пользователей — включается защита.
RAID_UNIQUE_SENDERS = 5

# Критический режим
CRITICAL_UNIQUE_SENDERS = 10

# Автоблокировка участников подозрительного рейда
AUTO_BLOCK = True

# Автоматически удалять сообщения подозрительных пользователей
AUTO_DELETE = True

# Автоматически завершать неизвестные Telegram-сессии
AUTO_KILL_NEW_SESSIONS = True

# Период проверки новых сессий
SESSION_CHECK_INTERVAL = 60


# =========================================================
# APP
# =========================================================

app = FastAPI()

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

user_client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
)


# =========================================================
# STATE
# =========================================================

started_at = time.time()

protection_enabled = True
emergency_mode = False

attack_started_at = None
last_attack_time = None

incoming_events = deque(maxlen=5000)

sender_events = defaultdict(deque)

blocked_users = set()
known_sessions = set()

attack_count = 0


# =========================================================
# HELPERS
# =========================================================

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def admin_only(message: Message) -> bool:
    return message.from_user and message.from_user.id == ADMIN_ID


async def admin_alert(text: str):
    if not ADMIN_ID:
        return

    with suppress(Exception):
        await bot.send_message(ADMIN_ID, text)


def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛡 Состояние",
                    callback_data="status"
                ),
                InlineKeyboardButton(
                    text="🚨 Атака",
                    callback_data="attack"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⚡ Emergency",
                    callback_data="emergency"
                ),
                InlineKeyboardButton(
                    text="🔐 Сессии",
                    callback_data="sessions"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧪 Симуляция",
                    callback_data="simulation"
                ),
                InlineKeyboardButton(
                    text="📊 История",
                    callback_data="history"
                ),
            ],
        ]
    )


def protection_status():
    if emergency_mode:
        return "🔴 EMERGENCY"

    if attack_started_at:
        return "🟠 DEFENSE"

    if protection_enabled:
        return "🟢 ACTIVE"

    return "⚪ OFF"


def get_status_text():
    uptime = int(time.time() - started_at)

    if attack_started_at:
        attack_time = int(time.time() - attack_started_at)
        attack_line = f"{attack_time} сек."
    else:
        attack_line = "нет"

    return (
        "🛡 <b>SENTINEL-X</b>\n\n"

        f"Система: <b>{protection_status()}</b>\n"
        f"Автозащита: <b>{'ON' if protection_enabled else 'OFF'}</b>\n"
        f"Emergency Shield: <b>{'ACTIVE' if emergency_mode else 'READY'}</b>\n\n"

        f"Активная атака: <b>{'ДА' if attack_started_at else 'НЕТ'}</b>\n"
        f"Текущая атака: <b>{attack_line}</b>\n"
        f"Всего инцидентов: <b>{attack_count}</b>\n"
        f"Заблокировано: <b>{len(blocked_users)}</b>\n\n"

        f"Uptime: <code>{uptime} сек.</code>"
    )


# =========================================================
# RAID ENGINE
# =========================================================

def register_sender(sender_id: int):
    current = time.time()

    q = sender_events[sender_id]
    q.append(current)

    cutoff = current - RAID_WINDOW

    while q and q[0] < cutoff:
        q.popleft()


def active_senders():
    current = time.time()
    result = set()

    for sender_id, q in list(sender_events.items()):
        cutoff = current - RAID_WINDOW

        while q and q[0] < cutoff:
            q.popleft()

        if q:
            result.add(sender_id)

    return result


async def activate_defense(reason: str):
    global attack_started_at
    global last_attack_time
    global attack_count

    if attack_started_at is None:
        attack_started_at = time.time()
        attack_count += 1

        await admin_alert(
            "🚨 <b>SENTINEL-X: ОБНАРУЖЕНА АТАКА</b>\n\n"
            f"Причина: <b>{reason}</b>\n"
            f"Время: <code>{now()}</code>\n\n"
            "🛡 DEFENSE MODE активирован."
        )

    last_attack_time = time.time()


async def activate_emergency(reason: str):
    global emergency_mode

    if not emergency_mode:
        emergency_mode = True

        await admin_alert(
            "🔴 <b>EMERGENCY SHIELD</b>\n\n"
            f"Причина: <b>{reason}</b>\n\n"
            "Автоматическая защита переведена "
            "в критический режим."
        )


async def attack_cleanup():
    global attack_started_at
    global emergency_mode

    while True:
        await asyncio.sleep(5)

        if attack_started_at is None:
            continue

        active = active_senders()

        if not active:
            if last_attack_time:
                if time.time() - last_attack_time > 15:
                    duration = int(time.time() - attack_started_at)

                    await admin_alert(
                        "🟢 <b>АТАКА ЗАВЕРШЕНА</b>\n\n"
                        f"Продолжительность: <b>{duration} сек.</b>\n"
                        f"Время окончания: <code>{now()}</code>\n\n"
                        "SENTINEL-X вернулся в обычный режим."
                    )

                    attack_started_at = None
                    emergency_mode = False


# =========================================================
# TELEGRAM ACCOUNT MONITOR
# =========================================================

@user_client.on(events.NewMessage(incoming=True))
async def incoming_message_handler(event):

    if not protection_enabled:
        return

    sender = await event.get_sender()

    if not sender:
        return

    sender_id = getattr(sender, "id", None)

    if not sender_id:
        return

    # Не считаем сообщения самого владельца
    if sender_id == ADMIN_ID:
        return

    register_sender(sender_id)

    incoming_events.append(
        {
            "time": time.time(),
            "sender": sender_id,
            "message_id": event.message.id,
        }
    )

    active = active_senders()

    # -----------------------------------------------------
    # RAID DETECTION
    # -----------------------------------------------------

    if len(active) >= RAID_UNIQUE_SENDERS:

        await activate_defense(
            f"{len(active)} разных отправителей "
            f"за {RAID_WINDOW} секунд"
        )

    if len(active) >= CRITICAL_UNIQUE_SENDERS:

        await activate_emergency(
            f"критический всплеск: {len(active)} отправителей"
        )

    # -----------------------------------------------------
    # AUTO BLOCK
    # -----------------------------------------------------

    if AUTO_BLOCK and attack_started_at:

        if sender_id not in blocked_users:

            # Добавляем в блок только при активной атаке
            # и после срабатывания нескольких признаков.
            if len(active) >= RAID_UNIQUE_SENDERS:

                try:
                    await user_client(
                        functions.contacts.BlockRequest(
                            id=sender_id
                        )
                    )

                    blocked_users.add(sender_id)

                    await admin_alert(
                        "🚫 <b>ПОДОЗРИТЕЛЬНЫЙ ОТПРАВИТЕЛЬ ЗАБЛОКИРОВАН</b>\n\n"
                        f"ID: <code>{sender_id}</code>\n"
                        f"Время: <code>{now()}</code>"
                    )

                except Exception as e:
                    await admin_alert(
                        "⚠️ Не удалось заблокировать "
                        f"<code>{sender_id}</code>\n"
                        f"<code>{type(e).__name__}</code>"
                    )

    # -----------------------------------------------------
    # AUTO DELETE
    # -----------------------------------------------------

    if AUTO_DELETE and emergency_mode:

        with suppress(Exception):
            await event.delete()


# =========================================================
# SESSION MONITOR
# =========================================================

async def get_sessions():
    result = await user_client(
        functions.account.GetAuthorizationsRequest()
    )

    return result.authorizations


async def check_sessions():

    global known_sessions

    await asyncio.sleep(10)

    while True:

        try:
            sessions = await get_sessions()

            current_hashes = {
                getattr(session, "hash", None)
                for session in sessions
            }

            current_hashes.discard(None)

            # Первый запуск — просто запоминаем существующие
            if not known_sessions:
                known_sessions = current_hashes.copy()

            else:

                new_sessions = current_hashes - known_sessions

                for session_hash in new_sessions:

                    session = next(
                        (
                            s for s in sessions
                            if getattr(s, "hash", None) == session_hash
                        ),
                        None
                    )

                    if not session:
                        continue

                    device = getattr(session, "device", "unknown")
                    platform = getattr(session, "platform", "unknown")
                    country = getattr(session, "country", "unknown")
                    ip = getattr(session, "ip", "unknown")

                    await admin_alert(
                        "🔐 <b>НОВАЯ СЕССИЯ</b>\n\n"
                        f"Устройство: <code>{device}</code>\n"
                        f"Платформа: <code>{platform}</code>\n"
                        f"IP: <code>{ip}</code>\n"
                        f"Страна: <code>{country}</code>\n\n"
                        "SENTINEL-X обнаружил новую авторизацию."
                    )

                    if AUTO_KILL_NEW_SESSIONS:

                        try:

                            await user_client(
                                functions.account.ResetAuthorizationRequest(
                                    hash=session_hash
                                )
                            )

                            await admin_alert(
                                "🛡 <b>НОВАЯ СЕССИЯ ЗАВЕРШЕНА</b>\n\n"
                                "Автоматическая защита сработала."
                            )

                        except Exception as e:

                            await admin_alert(
                                "⚠️ <b>Не удалось завершить новую сессию</b>\n"
                                f"<code>{type(e).__name__}</code>"
                            )

                known_sessions = current_hashes

        except FloodWaitError as e:

            await asyncio.sleep(e.seconds)

        except Exception as e:

            await admin_alert(
                "⚠️ Ошибка мониторинга сессий:\n"
                f"<code>{type(e).__name__}</code>"
            )

        await asyncio.sleep(SESSION_CHECK_INTERVAL)


# =========================================================
# TELEGRAM BOT
# =========================================================

@dp.message(CommandStart())
async def start(message: Message):

    if not admin_only(message):
        await message.answer(
            "⛔ Этот бот является приватной системой управления."
        )
        return

    await message.answer(
        "🛡 <b>SENTINEL-X</b>\n\n"
        "Система автоматической защиты от атак "
        "и подозрительной активности.\n\n"
        "Выбери действие:",
        reply_markup=main_keyboard()
    )


@dp.callback_query(F.data == "status")
async def callback_status(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        return

    await callback.message.edit_text(
        get_status_text(),
        reply_markup=main_keyboard()
    )

    await callback.answer()


@dp.callback_query(F.data == "attack")
async def callback_attack(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        return

    active = active_senders()

    text = (
        "🚨 <b>ТЕКУЩАЯ АТАКА</b>\n\n"
        f"Статус: <b>{'ACTIVE' if attack_started_at else 'нет'}</b>\n"
        f"Активных отправителей: <b>{len(active)}</b>\n"
        f"Emergency: <b>{'ON' if emergency_mode else 'OFF'}</b>\n"
        f"Заблокировано: <b>{len(blocked_users)}</b>"
    )

    await callback.message.edit_text(
        text,
        reply_markup=main_keyboard()
    )

    await callback.answer()


@dp.callback_query(F.data == "emergency")
async def callback_emergency(callback: CallbackQuery):

    global emergency_mode

    if callback.from_user.id != ADMIN_ID:
        return

    emergency_mode = not emergency_mode

    if emergency_mode:

        await admin_alert(
            "🔴 Emergency Shield включён вручную."
        )

    await callback.message.edit_text(
        "⚡ <b>EMERGENCY SHIELD</b>\n\n"
        f"Состояние: "
        f"<b>{'ACTIVE' if emergency_mode else 'READY'}</b>\n\n"
        "В критическом режиме подозрительные "
        "входящие сообщения автоматически удаляются.",
        reply_markup=main_keyboard()
    )

    await callback.answer()


@dp.callback_query(F.data == "sessions")
async def callback_sessions(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        return

    try:

        sessions = await get_sessions()

        lines = [
            "🔐 <b>АКТИВНЫЕ СЕССИИ</b>\n"
        ]

        for i, session in enumerate(sessions, 1):

            device = getattr(session, "device", "?")
            platform = getattr(session, "platform", "?")
            ip = getattr(session, "ip", "?")
            current = getattr(session, "current", False)

            lines.append(
                f"{i}. "
                f"{'🟢' if current else '⚪'} "
                f"{device} / {platform}\n"
                f"   IP: <code>{ip}</code>"
            )

        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=main_keyboard()
        )

    except Exception as e:

        await callback.message.edit_text(
            "⚠️ Ошибка получения сессий:\n"
            f"<code>{type(e).__name__}</code>",
            reply_markup=main_keyboard()
        )

    await callback.answer()


@dp.callback_query(F.data == "simulation")
async def callback_simulation(callback: CallbackQuery):

    global attack_started_at
    global last_attack_time
    global emergency_mode

    if callback.from_user.id != ADMIN_ID:
        return

    attack_started_at = time.time()
    last_attack_time = time.time()

    await callback.message.edit_text(
        "🧪 <b>СИМУЛЯЦИЯ АТАКИ</b>\n\n"
        "Тестовый инцидент создан.\n\n"
        "Проверяем:\n"
        "• Defense Engine\n"
        "• Emergency Shield\n"
        "• уведомления\n"
        "• Attack Timeline",
        reply_markup=main_keyboard()
    )

    await admin_alert(
        "🧪 <b>SIMULATION</b>\n\n"
        "Тестовая атака запущена вручную."
    )

    await callback.answer()


@dp.callback_query(F.data == "history")
async def callback_history(callback: CallbackQuery):

    if callback.from_user.id != ADMIN_ID:
        return

    events_count = len(incoming_events)

    await callback.message.edit_text(
        "📊 <b>ИСТОРИЯ</b>\n\n"
        f"Событий в памяти: <b>{events_count}</b>\n"
        f"Атак обнаружено: <b>{attack_count}</b>\n"
        f"Заблокировано: <b>{len(blocked_users)}</b>\n\n"
        f"Последняя проверка: <code>{now()}</code>",
        reply_markup=main_keyboard()
    )

    await callback.answer()


# =========================================================
# HEALTH SERVER
# =========================================================

@app.get("/")
async def root():

    return {
        "service": "SENTINEL-X",
        "status": "online",
        "protection": protection_status(),
        "attacks": attack_count,
        "blocked": len(blocked_users),
    }


@app.get("/health")
async def health():

    return {
        "status": "ok",
        "service": "sentinel-x",
        "protection": protection_enabled,
        "emergency": emergency_mode,
    }


# =========================================================
# STARTUP
# =========================================================

async def start_telegram():

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")

    if not ADMIN_ID:
        raise RuntimeError("ADMIN_ID is not configured")

    if not API_ID or not API_HASH:
        raise RuntimeError("API_ID/API_HASH are not configured")

    if not SESSION_STRING:
        raise RuntimeError("SESSION_STRING is not configured")

    await user_client.start()

    me = await user_client.get_me()

    await admin_alert(
        "🟢 <b>SENTINEL-X запущен</b>\n\n"
        f"Аккаунт: <b>{me.first_name or 'Unknown'}</b>\n"
        f"ID: <code>{me.id}</code>\n\n"
        "🛡 Автозащита активна."
    )


async def bot_worker():

    await start_telegram()

    asyncio.create_task(check_sessions())
    asyncio.create_task(attack_cleanup())

    await dp.start_polling(bot)


async def web_worker():

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )

    server = uvicorn.Server(config)

    await server.serve()


async def main():

    await asyncio.gather(
        bot_worker(),
        web_worker(),
    )


if __name__ == "__main__":
    asyncio.run(main())