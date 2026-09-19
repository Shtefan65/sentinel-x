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


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

PORT = int(os.getenv("PORT", "10000"))


# ============================================================
# PROTECTION SETTINGS
# ============================================================

RAID_WINDOW = 10

# Сколько разных отправителей за 10 секунд
# считается атакой.
RAID_UNIQUE_SENDERS = 5

# Критический уровень.
CRITICAL_UNIQUE_SENDERS = 10

# Автоматически блокировать подозрительных отправителей
AUTO_BLOCK = True

# Удалять входящие сообщения во время Emergency
AUTO_DELETE = True

# Автоматически завершать новые Telegram-сессии
#
# Для начала оставляем False.
# Иначе SENTINEL-X может завершить новую
# доверенную сессию при её появлении.
AUTO_KILL_NEW_SESSIONS = False

SESSION_CHECK_INTERVAL = 60


# ============================================================
# TELEGRAM / FASTAPI
# ============================================================

app = FastAPI()

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

user_client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
)


# ============================================================
# GLOBAL STATE
# ============================================================

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


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def admin_only(message: Message) -> bool:
    return (
        message.from_user
        and message.from_user.id == ADMIN_ID
    )


async def admin_alert(text: str):
    if not ADMIN_ID:
        return

    with suppress(Exception):
        await bot.send_message(
            ADMIN_ID,
            text
        )


# ============================================================
# MAIN MENU
# ============================================================

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


# ============================================================
# PROTECTION STATUS
# ============================================================

def protection_status():

    if emergency_mode:
        return "🔴 EMERGENCY"

    if attack_started_at:
        return "🟠 DEFENSE"

    if protection_enabled:
        return "🟢 ACTIVE"

    return "⚪ OFF"


# ============================================================
# STATUS SCREEN
# ============================================================

def get_status_text():

    uptime = int(time.time() - started_at)

    if attack_started_at:

        attack_time = int(
            time.time() - attack_started_at
        )

        attack_line = f"{attack_time} сек."

    else:

        attack_line = "нет"


    return (
        "🛡 SENTINEL-X\n"
        "\n"
        f"Система: {protection_status()}\n"
        f"Автозащита: {'ВКЛ' if protection_enabled else 'ВЫКЛ'}\n"
        f"Emergency Shield: "
        f"{'АКТИВЕН' if emergency_mode else 'ГОТОВ'}\n"
        "\n"
        f"Активная атака: "
        f"{'ДА' if attack_started_at else 'НЕТ'}\n"
        f"Текущая атака: {attack_line}\n"
        f"Всего инцидентов: {attack_count}\n"
        f"Заблокировано: {len(blocked_users)}\n"
        "\n"
        f"Время работы: {uptime} сек."
    )


# ============================================================
# RAID TRACKING
# ============================================================

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

    for sender_id, q in list(
        sender_events.items()
    ):

        cutoff = current - RAID_WINDOW

        while q and q[0] < cutoff:
            q.popleft()

        if q:
            result.add(sender_id)

    return result


# ============================================================
# DEFENSE MODE
# ============================================================

async def activate_defense(reason: str):

    global attack_started_at
    global last_attack_time
    global attack_count

    if attack_started_at is None:

        attack_started_at = time.time()

        attack_count += 1

        await admin_alert(
            "🚨 SENTINEL-X\n\n"
            "Обнаружена подозрительная активность.\n\n"
            f"Причина: {reason}\n"
            f"Время: {now()}\n\n"
            "🛡 Режим защиты активирован."
        )

    last_attack_time = time.time()


# ============================================================
# EMERGENCY
# ============================================================

async def activate_emergency(reason: str):

    global emergency_mode

    if not emergency_mode:

        emergency_mode = True

        await admin_alert(
            "🔴 EMERGENCY SHIELD\n\n"
            "Активирован критический режим защиты.\n\n"
            f"Причина: {reason}"
        )


# ============================================================
# ATTACK CLEANUP
# ============================================================

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

                    duration = int(
                        time.time() - attack_started_at
                    )

                    await admin_alert(
                        "🟢 SENTINEL-X\n\n"
                        "Атака завершена.\n\n"
                        f"Продолжительность: {duration} сек.\n"
                        f"Время окончания: {now()}\n\n"
                        "Система вернулась в обычный режим."
                    )

                    attack_started_at = None
                    emergency_mode = False


# ============================================================
# INCOMING MESSAGE MONITOR
# ============================================================

@user_client.on(
    events.NewMessage(incoming=True)
)
async def incoming_message_handler(event):

    if not protection_enabled:
        return

    sender = await event.get_sender()

    if not sender:
        return

    sender_id = getattr(
        sender,
        "id",
        None
    )

    if not sender_id:
        return

    # Игнорируем владельца
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

    # Обычная атака
    if len(active) >= RAID_UNIQUE_SENDERS:

        await activate_defense(
            f"{len(active)} разных отправителей "
            f"за {RAID_WINDOW} секунд"
        )

    # Критический режим
    if len(active) >= CRITICAL_UNIQUE_SENDERS:

        await activate_emergency(
            f"критический всплеск: "
            f"{len(active)} отправителей"
        )

    # Автоблокировка
    if AUTO_BLOCK and attack_started_at:

        if sender_id not in blocked_users:

            if len(active) >= RAID_UNIQUE_SENDERS:

                try:

                    await user_client(
                        functions.contacts.BlockRequest(
                            id=sender_id
                        )
                    )

                    blocked_users.add(sender_id)

                    await admin_alert(
                        "🚫 SENTINEL-X\n\n"
                        "Подозрительный отправитель заблокирован.\n\n"
                        f"ID: {sender_id}\n"
                        f"Время: {now()}"
                    )

                except Exception as e:

                    await admin_alert(
                        "⚠️ SENTINEL-X\n\n"
                        "Не удалось заблокировать отправителя.\n\n"
                        f"Тип ошибки: {type(e).__name__}"
                    )

    # Автоудаление
    if AUTO_DELETE and emergency_mode:

        with suppress(Exception):
            await event.delete()


# ============================================================
# SESSIONS
# ============================================================

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
                getattr(
                    session,
                    "hash",
                    None
                )
                for session in sessions
            }

            current_hashes.discard(None)

            # Первый запуск.
            # Запоминаем уже существующие сессии.
            if not known_sessions:

                known_sessions = (
                    current_hashes.copy()
                )

            else:

                new_sessions = (
                    current_hashes - known_sessions
                )

                for session_hash in new_sessions:

                    session = next(
                        (
                            s
                            for s in sessions
                            if getattr(
                                s,
                                "hash",
                                None
                            ) == session_hash
                        ),
                        None
                    )

                    if not session:
                        continue

                    device = getattr(
                        session,
                        "device",
                        "Неизвестно"
                    )

                    platform = getattr(
                        session,
                        "platform",
                        "Неизвестно"
                    )

                    country = getattr(
                        session,
                        "country",
                        "Неизвестно"
                    )

                    ip = getattr(
                        session,
                        "ip",
                        "Неизвестно"
                    )

                    await admin_alert(
                        "🔐 SENTINEL-X\n\n"
                        "Обнаружена новая сессия.\n\n"
                        f"Устройство: {device}\n"
                        f"Платформа: {platform}\n"
                        f"IP: {ip}\n"
                        f"Страна: {country}\n\n"
                        "SENTINEL-X обнаружил новую авторизацию."
                    )

                    # По умолчанию отключено.
                    if AUTO_KILL_NEW_SESSIONS:

                        try:

                            await user_client(
                                functions.account.ResetAuthorizationRequest(
                                    hash=session_hash
                                )
                            )

                            await admin_alert(
                                "🛡 SENTINEL-X\n\n"
                                "Новая сессия завершена.\n\n"
                                "Сработала автоматическая защита."
                            )

                        except Exception as e:

                            await admin_alert(
                                "⚠️ SENTINEL-X\n\n"
                                "Не удалось завершить новую сессию.\n\n"
                                f"Ошибка: {type(e).__name__}"
                            )

                known_sessions = (
                    current_hashes
                )

        except FloodWaitError as e:

            await asyncio.sleep(
                e.seconds
            )

        except Exception as e:

            await admin_alert(
                "⚠️ SENTINEL-X\n\n"
                "Ошибка мониторинга сессий.\n\n"
                f"Тип ошибки: {type(e).__name__}"
            )

        await asyncio.sleep(
            SESSION_CHECK_INTERVAL
        )


# ============================================================
# START COMMAND
# ============================================================

@dp.message(CommandStart())
async def start(message: Message):

    if not admin_only(message):

        await message.answer(
            "⛔ Доступ запрещён.\n"
            "Этот бот является приватной системой управления."
        )

        return

    await message.answer(
        "🛡 SENTINEL-X\n\n"
        "Система мониторинга и защиты Telegram-аккаунта.\n\n"
        "Выберите действие:",
        reply_markup=main_keyboard()
    )


# ============================================================
# STATUS
# ============================================================

@dp.callback_query(F.data == "status")
async def callback_status(
    callback: CallbackQuery
):

    if callback.from_user.id != ADMIN_ID:
        return

    await callback.message.edit_text(
        get_status_text(),
        reply_markup=main_keyboard()
    )

    await callback.answer()


# ============================================================
# ATTACK
# ============================================================

@dp.callback_query(F.data == "attack")
async def callback_attack(
    callback: CallbackQuery
):

    if callback.from_user.id != ADMIN_ID:
        return

    active = active_senders()

    text = (
        "🚨 ТЕКУЩАЯ АТАКА\n"
        "\n"
        f"Статус: "
        f"{'АКТИВНА' if attack_started_at else 'нет'}\n"
        f"Активных отправителей: {len(active)}\n"
        f"Emergency: "
        f"{'ВКЛ' if emergency_mode else 'ВЫКЛ'}\n"
        f"Заблокировано: {len(blocked_users)}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=main_keyboard()
    )

    await callback.answer()


# ============================================================
# EMERGENCY BUTTON
# ============================================================

@dp.callback_query(F.data == "emergency")
async def callback_emergency(
    callback: CallbackQuery
):

    global emergency_mode

    if callback.from_user.id != ADMIN_ID:
        return

    emergency_mode = not emergency_mode

    if emergency_mode:

        await admin_alert(
            "🔴 SENTINEL-X\n\n"
            "Emergency Shield включён вручную."
        )

    await callback.message.edit_text(
        "⚡ EMERGENCY SHIELD\n"
        "\n"
        f"Состояние: "
        f"{'АКТИВЕН' if emergency_mode else 'ГОТОВ'}\n"
        "\n"
        "В критическом режиме "
        "подозрительные входящие сообщения "
        "автоматически удаляются.",
        reply_markup=main_keyboard()
    )

    await callback.answer()


# ============================================================
# SESSIONS BUTTON
# ============================================================

@dp.callback_query(F.data == "sessions")
async def callback_sessions(
    callback: CallbackQuery
):

    if callback.from_user.id != ADMIN_ID:
        return

    try:

        sessions = await get_sessions()

        lines = [
            "🔐 АКТИВНЫЕ СЕССИИ",
            ""
        ]

        for i, session in enumerate(
            sessions,
            1
        ):

            device = getattr(
                session,
                "device",
                "Неизвестно"
            )

            platform = getattr(
                session,
                "platform",
                "Неизвестно"
            )

            ip = getattr(
                session,
                "ip",
                "Неизвестно"
            )

            current = getattr(
                session,
                "current",
                False
            )

            marker = "🟢" if current else "⚪"

            lines.append(
                f"{i}. {marker} "
                f"{device} / {platform}"
            )

            lines.append(
                f"   IP: {ip}"
            )

        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=main_keyboard()
        )

    except Exception as e:

        await callback.message.edit_text(
            "⚠️ Не удалось получить список сессий.\n\n"
            f"Ошибка: {type(e).__name__}",
            reply_markup=main_keyboard()
        )

    await callback.answer()


# ============================================================
# SIMULATION
# ============================================================

@dp.callback_query(F.data == "simulation")
async def callback_simulation(
    callback: CallbackQuery
):

    global attack_started_at
    global last_attack_time
    global emergency_mode

    if callback.from_user.id != ADMIN_ID:
        return

    attack_started_at = time.time()

    last_attack_time = time.time()

    await callback.message.edit_text(
        "🧪 СИМУЛЯЦИЯ АТАКИ\n"
        "\n"
        "Тестовый инцидент создан.\n"
        "\n"
        "Проверяются:\n"
        "• система обнаружения\n"
        "• Defense Mode\n"
        "• Emergency Shield\n"
        "• уведомления\n"
        "• история событий",
        reply_markup=main_keyboard()
    )

    await admin_alert(
        "🧪 SENTINEL-X\n\n"
        "Тестовая атака запущена вручную."
    )

    await callback.answer()


# ============================================================
# HISTORY
# ============================================================

@dp.callback_query(F.data == "history")
async def callback_history(
    callback: CallbackQuery
):

    if callback.from_user.id != ADMIN_ID:
        return

    events_count = len(
        incoming_events
    )

    await callback.message.edit_text(
        "📊 ИСТОРИЯ\n"
        "\n"
        f"Событий в памяти: {events_count}\n"
        f"Атак обнаружено: {attack_count}\n"
        f"Заблокировано: {len(blocked_users)}\n"
        "\n"
        f"Последняя проверка: {now()}",
        reply_markup=main_keyboard()
    )

    await callback.answer()


# ============================================================
# FASTAPI
# ============================================================

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


# ============================================================
# TELEGRAM START
# ============================================================

async def start_telegram():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не настроен"
        )

    if not ADMIN_ID:
        raise RuntimeError(
            "ADMIN_ID не настроен"
        )

    if not API_ID or not API_HASH:
        raise RuntimeError(
            "API_ID/API_HASH не настроены"
        )

    if not SESSION_STRING:
        raise RuntimeError(
            "SESSION_STRING не настроен"
        )

    # ВАЖНО:
    # connect(), а не start().
    #
    # Render не имеет интерактивного ввода,
    # поэтому start() здесь использовать не нужно.

    await user_client.connect()

    if not await user_client.is_user_authorized():

        raise RuntimeError(
            "SESSION_STRING недействительна "
            "или Telegram-сессия была отозвана"
        )

    me = await user_client.get_me()

    await admin_alert(
        "🟢 SENTINEL-X запущен\n"
        "\n"
        f"Аккаунт: {me.first_name or 'Неизвестно'}\n"
        f"ID: {me.id}\n"
        "\n"
        "🛡 Автозащита активна."
    )


# ============================================================
# BOT WORKER
# ============================================================

async def bot_worker():

    await start_telegram()

    asyncio.create_task(
        check_sessions()
    )

    asyncio.create_task(
        attack_cleanup()
    )

    await dp.start_polling(bot)


# ============================================================
# WEB WORKER
# ============================================================

async def web_worker():

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )

    server = uvicorn.Server(config)

    await server.serve()


# ============================================================
# MAIN
# ============================================================

async def main():

    await asyncio.gather(
        bot_worker(),
        web_worker(),
    )


if __name__ == "__main__":

    asyncio.run(main())