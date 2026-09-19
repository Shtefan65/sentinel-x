import asyncio
import hashlib
import os
import time

from collections import deque, defaultdict
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import FastAPI
import uvicorn

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from telethon import TelegramClient, events, functions
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
)

# ============================================================
# OPTIONAL POSTGRESQL
# ============================================================

try:
    import asyncpg
except ImportError:
    asyncpg = None


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

PORT = int(os.getenv("PORT", "10000"))

DATABASE_URL = os.getenv("DATABASE_URL", "")


# ============================================================
# PROTECTION CONFIG
# ============================================================

RAID_WINDOW = 10

RAID_UNIQUE_SENDERS = 5
CRITICAL_UNIQUE_SENDERS = 10

AUTO_BLOCK = True
AUTO_DELETE = True

# ВАЖНО:
# Оставляем False.
#
# Sentinel-X будет обнаруживать новые сессии,
# но не будет автоматически завершать их.
AUTO_KILL_NEW_SESSIONS = False

SESSION_CHECK_INTERVAL = 60


# Risk Engine

RISK_ATTACK = 40
RISK_CRITICAL = 70
RISK_PANIC = 90

RISK_DECAY_SECONDS = 30

DUPLICATE_WINDOW = 20

MAX_EVENTS = 10000
MAX_TIMELINE = 5000


# ============================================================
# FASTAPI / TELEGRAM
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
# DATABASE
# ============================================================

db_pool = None


async def init_database():
    global db_pool

    if not DATABASE_URL:
        return

    if asyncpg is None:
        print("DATABASE_URL задан, но asyncpg не установлен.")
        return

    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=3,
        )

        async with db_pool.acquire() as conn:

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sentinel_events (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL,
                    event_type TEXT NOT NULL,
                    sender_id BIGINT,
                    message_hash TEXT,
                    risk INTEGER,
                    details TEXT
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sentinel_attacks (
                    id BIGSERIAL PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL,
                    ended_at TIMESTAMPTZ,
                    duration_seconds INTEGER,
                    max_risk INTEGER
                )
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sentinel_whitelist (
                    user_id BIGINT PRIMARY KEY,
                    added_at TIMESTAMPTZ NOT NULL
                )
                """
            )

        print("PostgreSQL: подключение успешно.")

    except Exception as e:
        print(
            "PostgreSQL error:",
            type(e).__name__,
            str(e),
        )
        db_pool = None


async def db_event(
    event_type,
    sender_id=None,
    message_hash=None,
    risk=None,
    details=None,
):
    if not db_pool:
        return

    try:

        async with db_pool.acquire() as conn:

            await conn.execute(
                """
                INSERT INTO sentinel_events
                (
                    created_at,
                    event_type,
                    sender_id,
                    message_hash,
                    risk,
                    details
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                datetime.now(timezone.utc),
                event_type,
                sender_id,
                message_hash,
                risk,
                details,
            )

    except Exception as e:

        print(
            "DB event error:",
            type(e).__name__,
        )


# ============================================================
# GLOBAL STATE
# ============================================================

started_at = time.time()

protection_enabled = True

emergency_mode = False
panic_mode = False

attack_started_at = None
last_attack_time = None

attack_count = 0

max_risk = 0

current_risk = 0

incoming_events = deque(
    maxlen=MAX_EVENTS
)

timeline = deque(
    maxlen=MAX_TIMELINE
)

sender_events = defaultdict(deque)

sender_message_count = defaultdict(int)

message_hash_events = defaultdict(deque)

blocked_users = set()

whitelist = set()

known_sessions = set()

background_tasks = []


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def utc_datetime():
    return datetime.now(timezone.utc)


def admin_only(message: Message):
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
            text,
        )


def add_timeline(
    event_type,
    description,
    risk=None,
):
    timeline.append(
        {
            "time": now(),
            "type": event_type,
            "description": description,
            "risk": risk,
        }
    )


# ============================================================
# KEYBOARD
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(
                    text="🛡 Состояние",
                    callback_data="status",
                ),
                InlineKeyboardButton(
                    text="🚨 Атака",
                    callback_data="attack",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="⚡ Emergency",
                    callback_data="emergency",
                ),
                InlineKeyboardButton(
                    text="☢️ Panic",
                    callback_data="panic",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="🔐 Сессии",
                    callback_data="sessions",
                ),
                InlineKeyboardButton(
                    text="👥 Доверенные",
                    callback_data="whitelist",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="📊 Timeline",
                    callback_data="timeline",
                ),
                InlineKeyboardButton(
                    text="📈 Risk",
                    callback_data="risk",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="🧪 Симуляция",
                    callback_data="simulation",
                ),
                InlineKeyboardButton(
                    text="🔧 Диагностика",
                    callback_data="diagnostics",
                ),
            ],

            [
                InlineKeyboardButton(
                    text="📚 История",
                    callback_data="history",
                ),
            ],
        ]
    )


# ============================================================
# PROTECTION STATUS
# ============================================================

def protection_status():

    if panic_mode:
        return "☢️ PANIC"

    if emergency_mode:
        return "🔴 EMERGENCY"

    if attack_started_at:
        return "🟠 DEFENSE"

    if protection_enabled:
        return "🟢 ACTIVE"

    return "⚪ OFF"


# ============================================================
# RISK ENGINE
# ============================================================

def calculate_risk():

    global current_risk
    global max_risk

    now_ts = time.time()

    active = set()

    messages_10s = 0
    messages_30s = 0

    for sender_id, queue in list(
        sender_events.items()
    ):

        while queue and queue[0] < now_ts - 30:
            queue.popleft()

        if queue:
            active.add(sender_id)

        for timestamp in queue:

            if timestamp >= now_ts - 10:
                messages_10s += 1

            if timestamp >= now_ts - 30:
                messages_30s += 1

    unique_senders = len(active)

    risk = 0

    # Массовость
    risk += min(
        unique_senders * 6,
        40,
    )

    # Скорость сообщений
    risk += min(
        messages_10s * 2,
        20,
    )

    # Общий всплеск
    risk += min(
        messages_30s,
        15,
    )

    # Уже заблокированные
    if len(blocked_users) > 0:
        risk += min(
            len(blocked_users) * 2,
            10,
        )

    # Emergency
    if emergency_mode:
        risk += 15

    # Panic
    if panic_mode:
        risk = 100

    current_risk = max(
        0,
        min(risk, 100),
    )

    if current_risk > max_risk:
        max_risk = current_risk

    return current_risk


def risk_level():

    risk = calculate_risk()

    if risk >= RISK_PANIC:
        return "☢️ CRITICAL"

    if risk >= RISK_CRITICAL:
        return "🔴 HIGH"

    if risk >= RISK_ATTACK:
        return "🟠 ELEVATED"

    if risk >= 20:
        return "🟡 WATCH"

    return "🟢 NORMAL"


# ============================================================
# STATUS
# ============================================================

def get_status_text():

    uptime = int(
        time.time() - started_at
    )

    risk = calculate_risk()

    if attack_started_at:

        attack_time = int(
            time.time() - attack_started_at
        )

        attack_line = (
            f"{attack_time} сек."
        )

    else:

        attack_line = "нет"

    return (
        "🛡 SENTINEL-X\n"
        "\n"
        f"Система: {protection_status()}\n"
        f"Уровень риска: {risk_level()}\n"
        f"Risk Score: {risk}/100\n"
        "\n"
        f"Автозащита: "
        f"{'ВКЛ' if protection_enabled else 'ВЫКЛ'}\n"
        f"Emergency Shield: "
        f"{'АКТИВЕН' if emergency_mode else 'ГОТОВ'}\n"
        f"Panic Mode: "
        f"{'АКТИВЕН' if panic_mode else 'ГОТОВ'}\n"
        "\n"
        f"Активная атака: "
        f"{'ДА' if attack_started_at else 'НЕТ'}\n"
        f"Текущая атака: {attack_line}\n"
        f"Всего атак: {attack_count}\n"
        f"Заблокировано: {len(blocked_users)}\n"
        f"Доверенных: {len(whitelist)}\n"
        "\n"
        f"Максимальный Risk: {max_risk}/100\n"
        f"Время работы: {uptime} сек."
    )


# ============================================================
# SENDER TRACKING
# ============================================================

def register_sender(sender_id):

    current = time.time()

    queue = sender_events[
        sender_id
    ]

    queue.append(current)

    sender_message_count[
        sender_id
    ] += 1

    cutoff = (
        current - RAID_WINDOW
    )

    while queue and queue[0] < cutoff:
        queue.popleft()


def active_senders():

    current = time.time()

    result = set()

    for sender_id, queue in list(
        sender_events.items()
    ):

        cutoff = (
            current - RAID_WINDOW
        )

        while queue and queue[0] < cutoff:
            queue.popleft()

        if queue:
            result.add(sender_id)

    return result


# ============================================================
# MESSAGE FINGERPRINT
# ============================================================

def message_fingerprint(text):

    if not text:
        return None

    normalized = (
        text.strip()
        .lower()
        .replace("\n", " ")
    )

    if not normalized:
        return None

    return hashlib.sha256(
        normalized.encode(
            "utf-8",
            errors="ignore",
        )
    ).hexdigest()


def count_recent_duplicates(
    message_hash,
):

    if not message_hash:
        return 0

    current = time.time()

    queue = message_hash_events[
        message_hash
    ]

    while queue and (
        queue[0] <
        current - DUPLICATE_WINDOW
    ):
        queue.popleft()

    return len(queue)


def register_message_hash(
    message_hash,
):

    if not message_hash:
        return

    current = time.time()

    queue = message_hash_events[
        message_hash
    ]

    queue.append(current)


# ============================================================
# ATTACK DETECTION
# ============================================================

async def activate_defense(
    reason: str,
):

    global attack_started_at
    global last_attack_time
    global attack_count

    if attack_started_at is None:

        attack_started_at = time.time()

        attack_count += 1

        add_timeline(
            "ATTACK",
            reason,
            current_risk,
        )

        await db_event(
            "attack_started",
            risk=current_risk,
            details=reason,
        )

        await admin_alert(
            "🚨 SENTINEL-X\n"
            "\n"
            "Обнаружена подозрительная активность.\n"
            "\n"
            f"Причина: {reason}\n"
            f"Risk Score: {current_risk}/100\n"
            f"Время: {now()}\n"
            "\n"
            "🛡 Режим защиты активирован."
        )

    last_attack_time = time.time()


async def activate_emergency(
    reason: str,
):

    global emergency_mode

    if emergency_mode:
        return

    emergency_mode = True

    add_timeline(
        "EMERGENCY",
        reason,
        current_risk,
    )

    await db_event(
        "emergency",
        risk=current_risk,
        details=reason,
    )

    await admin_alert(
        "🔴 SENTINEL-X\n"
        "\n"
        "EMERGENCY SHIELD активирован.\n"
        "\n"
        f"Причина: {reason}\n"
        f"Risk Score: {current_risk}/100\n"
        f"Время: {now()}\n"
        "\n"
        "Система перешла в критический режим."
    )


async def activate_panic(
    reason="ручная активация",
):

    global panic_mode
    global emergency_mode

    panic_mode = True
    emergency_mode = True

    add_timeline(
        "PANIC",
        reason,
        100,
    )

    await db_event(
        "panic",
        risk=100,
        details=reason,
    )

    await admin_alert(
        "☢️ SENTINEL-X\n"
        "\n"
        "PANIC MODE АКТИВИРОВАН.\n"
        "\n"
        "Максимальный уровень локальной защиты включён."
    )


# ============================================================
# ATTACK CLEANUP
# ============================================================

async def attack_cleanup():

    global attack_started_at
    global emergency_mode
    global panic_mode

    while True:

        await asyncio.sleep(5)

        if attack_started_at is None:
            continue

        active = active_senders()

        risk = calculate_risk()

        if not active and not panic_mode:

            if last_attack_time:

                if (
                    time.time()
                    - last_attack_time
                    > 15
                ):

                    duration = int(
                        time.time()
                        - attack_started_at
                    )

                    add_timeline(
                        "RECOVERY",
                        "Атака завершена",
                        risk,
                    )

                    await db_event(
                        "attack_finished",
                        risk=risk,
                        details=(
                            f"duration={duration}"
                        ),
                    )

                    await admin_alert(
                        "🟢 SENTINEL-X\n"
                        "\n"
                        "Атака завершена.\n"
                        "\n"
                        f"Продолжительность: {duration} сек.\n"
                        f"Максимальный Risk: {max_risk}/100\n"
                        f"Время окончания: {now()}\n"
                        "\n"
                        "Система вернулась в обычный режим."
                    )

                    attack_started_at = None
                    emergency_mode = False


# ============================================================
# INCOMING MESSAGE MONITOR
# ============================================================

@user_client.on(
    events.NewMessage(
        incoming=True
    )
)
async def incoming_message_handler(
    event,
):

    if not protection_enabled:
        return

    sender = await event.get_sender()

    if not sender:
        return

    sender_id = getattr(
        sender,
        "id",
        None,
    )

    if not sender_id:
        return

    if sender_id == ADMIN_ID:
        return

    # Trusted users
    if sender_id in whitelist:

        incoming_events.append(
            {
                "time": time.time(),
                "sender": sender_id,
                "trusted": True,
            }
        )

        return

    message_text = (
        event.raw_text or ""
    )

    message_hash = (
        message_fingerprint(
            message_text
        )
    )

    duplicates = (
        count_recent_duplicates(
            message_hash
        )
    )

    register_message_hash(
        message_hash
    )

    register_sender(
        sender_id
    )

    incoming_events.append(
        {
            "time": time.time(),
            "sender": sender_id,
            "message_id": event.message.id,
            "hash": message_hash,
        }
    )

    risk = calculate_risk()

    # Повторяющийся контент
    if duplicates >= 3:

        risk = min(
            100,
            risk + 15,
        )

        add_timeline(
            "DUPLICATES",
            f"Повторяющееся сообщение: {duplicates + 1}",
            risk,
        )

    active = active_senders()

    # Массовый всплеск
    if (
        len(active)
        >= RAID_UNIQUE_SENDERS
    ):

        await activate_defense(
            f"{len(active)} разных отправителей "
            f"за {RAID_WINDOW} секунд"
        )

    # Критический всплеск
    if (
        len(active)
        >= CRITICAL_UNIQUE_SENDERS
    ):

        await activate_emergency(
            f"критический всплеск: "
            f"{len(active)} отправителей"
        )

    # Risk Engine
    if risk >= RISK_CRITICAL:

        await activate_emergency(
            f"Risk Score достиг {risk}/100"
        )

    elif risk >= RISK_ATTACK:

        await activate_defense(
            f"Risk Score достиг {risk}/100"
        )

    # Автоблокировка
    if (
        AUTO_BLOCK
        and attack_started_at
        and sender_id not in blocked_users
    ):

        should_block = False

        if (
            len(active)
            >= RAID_UNIQUE_SENDERS
        ):
            should_block = True

        if duplicates >= 3:
            should_block = True

        if risk >= RISK_CRITICAL:
            should_block = True

        if should_block:

            try:

                await user_client(
                    functions.contacts.BlockRequest(
                        id=sender_id
                    )
                )

                blocked_users.add(
                    sender_id
                )

                add_timeline(
                    "BLOCK",
                    f"Пользователь {sender_id} заблокирован",
                    risk,
                )

                await db_event(
                    "block",
                    sender_id=sender_id,
                    message_hash=message_hash,
                    risk=risk,
                    details="automatic",
                )

                await admin_alert(
                    "🚫 SENTINEL-X\n"
                    "\n"
                    "Подозрительный пользователь заблокирован.\n"
                    "\n"
                    f"ID: {sender_id}\n"
                    f"Risk Score: {risk}/100\n"
                    f"Время: {now()}"
                )

            except Exception as e:

                await admin_alert(
                    "⚠️ SENTINEL-X\n"
                    "\n"
                    "Не удалось заблокировать пользователя.\n"
                    "\n"
                    f"ID: {sender_id}\n"
                    f"Ошибка: {type(e).__name__}"
                )

    # Emergency / Panic cleanup
    if (
        AUTO_DELETE
        and (
            emergency_mode
            or panic_mode
        )
    ):

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

            sessions = (
                await get_sessions()
            )

            current_hashes = {
                getattr(
                    session,
                    "hash",
                    None,
                )
                for session in sessions
            }

            current_hashes.discard(
                None
            )

            if not known_sessions:

                known_sessions = (
                    current_hashes.copy()
                )

            else:

                new_sessions = (
                    current_hashes
                    - known_sessions
                )

                for session_hash in new_sessions:

                    session = next(
                        (
                            s
                            for s in sessions
                            if getattr(
                                s,
                                "hash",
                                None,
                            )
                            == session_hash
                        ),
                        None,
                    )

                    if not session:
                        continue

                    device = getattr(
                        session,
                        "device",
                        "Неизвестно",
                    )

                    platform = getattr(
                        session,
                        "platform",
                        "Неизвестно",
                    )

                    country = getattr(
                        session,
                        "country",
                        "Неизвестно",
                    )

                    ip = getattr(
                        session,
                        "ip",
                        "Неизвестно",
                    )

                    add_timeline(
                        "SESSION",
                        f"Новая сессия: {device}",
                        50,
                    )

                    await db_event(
                        "new_session",
                        risk=50,
                        details=(
                            f"{device} / "
                            f"{platform} / "
                            f"{ip}"
                        ),
                    )

                    await admin_alert(
                        "🔐 SENTINEL-X\n"
                        "\n"
                        "Обнаружена новая Telegram-сессия.\n"
                        "\n"
                        f"Устройство: {device}\n"
                        f"Платформа: {platform}\n"
                        f"IP: {ip}\n"
                        f"Страна: {country}\n"
                        "\n"
                        "Проверьте, действительно ли эта авторизация ваша."
                    )

                    # Автоматическое завершение
                    # намеренно отключено.
                    if AUTO_KILL_NEW_SESSIONS:

                        try:

                            await user_client(
                                functions.account.ResetAuthorizationRequest(
                                    hash=session_hash
                                )
                            )

                            add_timeline(
                                "SESSION_KILL",
                                "Новая сессия завершена",
                                80,
                            )

                            await admin_alert(
                                "🛡 SENTINEL-X\n"
                                "\n"
                                "Новая сессия завершена автоматически."
                            )

                        except Exception as e:

                            await admin_alert(
                                "⚠️ SENTINEL-X\n"
                                "\n"
                                "Не удалось завершить новую сессию.\n"
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
                "⚠️ SENTINEL-X\n"
                "\n"
                "Ошибка мониторинга сессий.\n"
                f"Ошибка: {type(e).__name__}"
            )

        await asyncio.sleep(
            SESSION_CHECK_INTERVAL
        )


# ============================================================
# DIAGNOSTICS
# ============================================================

async def run_diagnostics():

    results = []

    # -------------------------
    # Config
    # -------------------------

    results.append(
        (
            "BOT_TOKEN",
            bool(BOT_TOKEN),
        )
    )

    results.append(
        (
            "ADMIN_ID",
            bool(ADMIN_ID),
        )
    )

    results.append(
        (
            "API_ID",
            bool(API_ID),
        )
    )

    results.append(
        (
            "API_HASH",
            bool(API_HASH),
        )
    )

    results.append(
        (
            "SESSION_STRING",
            bool(SESSION_STRING),
        )
    )

    # -------------------------
    # User Telegram session
    # -------------------------

    telegram_ok = False

    try:

        telegram_ok = (
            await user_client.is_user_authorized()
        )

    except Exception:
        telegram_ok = False

    results.append(
        (
            "Telegram-сессия",
            telegram_ok,
        )
    )

    # -------------------------
    # Bot
    # -------------------------

    bot_ok = False

    try:

        me = await bot.get_me()

        bot_ok = bool(
            getattr(
                me,
                "id",
                None,
            )
        )

    except Exception:
        bot_ok = False

    results.append(
        (
            "Telegram Bot API",
            bot_ok,
        )
    )

    # -------------------------
    # PostgreSQL
    # -------------------------

    if DATABASE_URL:

        results.append(
            (
                "PostgreSQL",
                db_pool is not None,
            )
        )

    else:

        results.append(
            (
                "PostgreSQL",
                None,
            )
        )

    # -------------------------
    # Protection modules
    # -------------------------

    results.append(
        (
            "Risk Engine",
            True,
        )
    )

    results.append(
        (
            "Attack Detector",
            True,
        )
    )

    results.append(
        (
            "Duplicate Detector",
            True,
        )
    )

    results.append(
        (
            "Smart Auto-Block",
            AUTO_BLOCK,
        )
    )

    results.append(
        (
            "Message Cleanup",
            AUTO_DELETE,
        )
    )

    results.append(
        (
            "Emergency Shield",
            True,
        )
    )

    results.append(
        (
            "Panic Mode",
            True,
        )
    )

    results.append(
        (
            "Session Guard",
            True,
        )
    )

    results.append(
        (
            "Timeline",
            True,
        )
    )

    # -------------------------
    # Format
    # -------------------------

    lines = [
        "🔧 SENTINEL-X — ДИАГНОСТИКА",
        "",
    ]

    passed = 0
    failed = 0
    skipped = 0

    for name, state in results:

        if state is True:

            icon = "🟢"
            passed += 1

        elif state is False:

            icon = "🔴"
            failed += 1

        else:

            icon = "⚪"
            skipped += 1

        lines.append(
            f"{icon} {name}"
        )

    lines.extend(
        [
            "",
            "ИТОГ",
            "",
            f"🟢 Работает: {passed}",
            f"🔴 Ошибки: {failed}",
            f"⚪ Не настроено: {skipped}",
            "",
            f"Risk Engine: {calculate_risk()}/100",
            f"Состояние: {protection_status()}",
        ]
    )

    return "\n".join(lines)


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start(
    message: Message,
):

    if not admin_only(message):

        await message.answer(
            "⛔ Доступ запрещён.\n"
            "Этот бот является приватной системой управления."
        )

        return

    await message.answer(
        "🛡 SENTINEL-X v2\n"
        "\n"
        "Система мониторинга и локальной защиты Telegram-аккаунта.\n"
        "\n"
        "Выберите действие:",
        reply_markup=main_keyboard(),
    )


# ============================================================
# WHITELIST COMMANDS
# ============================================================

@dp.message(Command("allow"))
async def allow_user(
    message: Message,
):

    if not admin_only(message):
        return

    parts = (
        message.text or ""
    ).split()

    if len(parts) != 2:

        await message.answer(
            "Использование:\n"
            "/allow USER_ID"
        )

        return

    try:

        user_id = int(parts[1])

    except ValueError:

        await message.answer(
            "❌ USER_ID должен быть числом."
        )

        return

    whitelist.add(
        user_id
    )

    await db_event(
        "whitelist_add",
        sender_id=user_id,
        details="manual",
    )

    await message.answer(
        "🟢 Пользователь добавлен в доверенные.\n\n"
        f"ID: {user_id}\n"
        f"Всего доверенных: {len(whitelist)}"
    )


@dp.message(Command("unallow"))
async def unallow_user(
    message: Message,
):

    if not admin_only(message):
        return

    parts = (
        message.text or ""
    ).split()

    if len(parts) != 2:

        await message.answer(
            "Использование:\n"
            "/unallow USER_ID"
        )

        return

    try:

        user_id = int(parts[1])

    except ValueError:

        await message.answer(
            "❌ USER_ID должен быть числом."
        )

        return

    whitelist.discard(
        user_id
    )

    await message.answer(
        "⚪ Пользователь удалён из доверенных.\n\n"
        f"ID: {user_id}"
    )


# ============================================================
# STATUS CALLBACK
# ============================================================

@dp.callback_query(
    F.data == "status"
)
async def callback_status(
    callback: CallbackQuery,
):

    if callback.from_user.id != ADMIN_ID:
        return

    await callback.message.edit_text(
        get_status_text(),
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# ============================================================
# ATTACK CALLBACK
# ============================================================

@dp.callback_query(
    F.data == "attack"
)
async def callback_attack(
    callback: CallbackQuery,
):

    if callback.from_user.id != ADMIN_ID:
        return

    active = active_senders()

    text = (
        "🚨 ТЕКУЩАЯ АТАКА\n"
        "\n"
        f"Статус: "
        f"{'АКТИВНА' if attack_started_at else 'нет'}\n"
        f"Risk Score: {calculate_risk()}/100\n"
        f"Уровень: {risk_level()}\n"
        f"Активных отправителей: {len(active)}\n"
        f"Emergency: "
        f"{'ВКЛ' if emergency_mode else 'ВЫКЛ'}\n"
        f"Panic: "
        f"{'ВКЛ' if panic_mode else 'ВЫКЛ'}\n"
        f"Заблокировано: {len(blocked_users)}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# ============================================================
# EMERGENCY CALLBACK
# ============================================================

@dp.callback_query(
    F.data == "emergency"
)
async def callback_emergency(
    callback: CallbackQuery,
):

    global emergency_mode

    if callback.from_user.id != ADMIN_ID:
        return

    emergency_mode = not emergency_mode

    if emergency_mode:

        add_timeline(
            "EMERGENCY",
            "Emergency Shield включён вручную",
            calculate_risk(),
        )

        await admin_alert(
            "🔴 SENTINEL-X\n\n"
            "Emergency Shield включён вручную."
        )

    else:

        add_timeline(
            "EMERGENCY",
            "Emergency Shield выключен",
            calculate_risk(),
        )

    await callback.message.edit_text(
        "⚡ EMERGENCY SHIELD\n"
        "\n"
        f"Состояние: "
        f"{'АКТИВЕН' if emergency_mode else 'ГОТОВ'}\n"
        "\n"
        "В критическом режиме "
        "входящие сообщения автоматически удаляются.",
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# ============================================================
# PANIC CALLBACK
# ============================================================

@dp.callback_query(
    F.data == "panic"
)
async def callback_panic(
    callback: CallbackQuery,
):

    global panic_mode
    global emergency_mode

    if callback.from_user.id != ADMIN_ID:
        return

    panic_mode = not panic_mode

    if panic_mode:

        emergency_mode = True

        add_timeline(
            "PANIC",
            "Panic Mode включён вручную",
            100,
        )

        await admin_alert(
            "☢️ SENTINEL-X\n\n"
            "PANIC MODE включён."
        )

    else:

        emergency_mode = False

        add_timeline(
            "PANIC",
            "Panic Mode выключен",
            calculate_risk(),
        )

        await admin_alert(
            "🟢 SENTINEL-X\n\n"
            "Panic Mode выключен."
        )

    await callback.message.edit_text(
        "☢️ PANIC MODE\n"
        "\n"
        f"Состояние: "
        f"{'АКТИВЕН' if panic_mode else 'ГОТОВ'}\n"
        "\n"
        "При активации используется максимальный "
        "уровень локальной защиты.",
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# ============================================================
# SESSIONS CALLBACK
# ============================================================

@dp.callback_query(
    F.data == "sessions"
)
async def callback_sessions(
    callback: CallbackQuery,
):

    if callback.from_user.id != ADMIN_ID:
        return

    try:

        sessions = (
            await get_sessions()
        )

        lines = [
            "🔐 АКТИВНЫЕ СЕССИИ",
            "",
        ]

        for i, session in enumerate(
            sessions,
            1,
        ):

            device = getattr(
                session,
                "device",
                "Неизвестно",
            )

            platform = getattr(
                session,
                "platform",
                "Неизвестно",
            )

            ip = getattr(
                session,
                "ip",
                "Неизвестно",
            )

            current = getattr(
                session,
                "current",
                False,
            )

            marker = (
                "🟢"
                if current
                else "⚪"
            )

            lines.append(
                f"{i}. {marker} "
                f"{device} / {platform}"
            )

            lines.append(
                f"   IP: {ip}"
            )

        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=main_keyboard(),
        )

    except Exception as e:

        await callback.message.edit_text(
            "⚠️ Не удалось получить список сессий.\n\n"
            f"Ошибка: {type(e).__name__}",
            reply_markup=main_keyboard(),
        )

    await callback.answer()


# ============================================================
# WHITELIST CALLBACK
# ============================================================

@dp.callback_query(
    F.data == "whitelist"
)
async def callback_whitelist(
    callback: CallbackQuery,
):

    if callback.from_user.id != ADMIN_ID:
        return

    lines = [
        "👥 ДОВЕРЕННЫЕ ПОЛЬЗОВАТЕЛИ",
        "",
    ]

    if not whitelist:

        lines.append(
            "Список пока пуст."
        )

    else:

        for user_id in sorted(
            whitelist
        ):

            lines.append(
                f"🟢 {user_id}"
            )

    lines.extend(
        [
            "",
            "Добавить:",
            "/allow USER_ID",
            "",
            "Удалить:",
            "/unallow USER_ID",
        ]
    )

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# ============================================================
# RISK CALLBACK
# ============================================================

@dp.callback_query(
    F.data == "risk"
)
async def callback_risk(
    callback: CallbackQuery,
):

    if callback.from_user.id != ADMIN_ID:
        return

    risk = calculate_risk()

    active = len(
        active_senders()
    )

    lines = [
        "📈 RISK ENGINE",
        "",
        f"Текущий Risk: {risk}/100",
        f"Уровень: {risk_level()}",
        f"Максимальный Risk: {max_risk}/100",
        "",
        f"Активных отправителей: {active}",
        f"Заблокировано: {len(blocked_users)}",
        f"Событий: {len(incoming_events)}",
        "",
        "Пороги:",
        f"🟠 Defense: {RISK_ATTACK}",
        f"🔴 Emergency: {RISK_CRITICAL}",
        f"☢️ Critical: {RISK_PANIC}",
    ]

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# ============================================================
# TIMELINE
# ============================================================

@dp.callback_query(
    F.data == "timeline"
)
async def callback_timeline(
    callback: CallbackQuery,
):

    if callback.from_user.id != ADMIN_ID:
        return

    if not timeline:

        text = (
            "📊 ATTACK TIMELINE\n"
            "\n"
            "Событий пока нет."
        )

    else:

        recent = list(
            timeline
        )[-15:]

        lines = [
            "📊 ATTACK TIMELINE",
            "",
        ]

        for item in recent:

            risk = item.get(
                "risk"
            )

            risk_text = (
                f" | Risk {risk}"
                if risk is not None
                else ""
            )

            lines.append(
                f"{item['time']}"
            )

            lines.append(
                f"{item['type']} — "
                f"{item['description']}"
                f"{risk_text}"
            )

            lines.append("")

        text = "\n".join(
            lines
        )

    await callback.message.edit_text(
        text,
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# ============================================================
# SIMULATION
# ============================================================

@dp.callback_query(
    F.data == "simulation"
)
async def callback_simulation(
    callback: CallbackQuery,
):

    global attack_started_at
    global last_attack_time

    if callback.from_user.id != ADMIN_ID:
        return

    # Сбрасываем старую тестовую атаку
    attack_started_at = time.time()

    last_attack_time = time.time()

    add_timeline(
        "SIMULATION",
        "Тестовая атака запущена",
        65,
    )

    await callback.message.edit_text(
        "🧪 СИМУЛЯЦИЯ\n"
        "\n"
        "Тестовая атака запущена.\n"
        "\n"
        "Проверяются:\n"
        "• Risk Engine\n"
        "• Attack Detector\n"
        "• Defense Mode\n"
        "• Emergency Shield\n"
        "• Timeline\n"
        "• уведомления\n"
        "• восстановление\n"
        "\n"
        "Сама симуляция ничего не блокирует.",
        reply_markup=main_keyboard(),
    )

    await admin_alert(
        "🧪 SENTINEL-X\n\n"
        "Тестовая атака запущена вручную."
    )

    # Через несколько секунд создаём
    # критический уровень именно как тест.
    async def simulation_worker():

        global emergency_mode

        await asyncio.sleep(3)

        emergency_mode = True

        add_timeline(
            "SIMULATION",
            "Тестовый Emergency Shield",
            85,
        )

        await admin_alert(
            "🧪 SENTINEL-X\n\n"
            "Симуляция достигла критического уровня.\n"
            "Emergency Shield проверен."
        )

        await asyncio.sleep(5)

        emergency_mode = False

        add_timeline(
            "SIMULATION",
            "Тестовый Emergency завершён",
            45,
        )

        await admin_alert(
            "🧪 SENTINEL-X\n\n"
            "Критическая часть симуляции завершена."
        )

    asyncio.create_task(
        simulation_worker()
    )

    await callback.answer()


# ============================================================
# DIAGNOSTICS CALLBACK
# ============================================================

@dp.callback_query(
    F.data == "diagnostics"
)
async def callback_diagnostics(
    callback: CallbackQuery,
):

    if callback.from_user.id != ADMIN_ID:
        return

    await callback.answer(
        "Запускаю диагностику..."
    )

    text = await run_diagnostics()

    await callback.message.edit_text(
        text,
        reply_markup=main_keyboard(),
    )


# ============================================================
# HISTORY
# ============================================================

@dp.callback_query(
    F.data == "history"
)
async def callback_history(
    callback: CallbackQuery,
):

    if callback.from_user.id != ADMIN_ID:
        return

    events_count = len(
        incoming_events
    )

    await callback.message.edit_text(
        "📚 ИСТОРИЯ\n"
        "\n"
        f"Событий в памяти: {events_count}\n"
        f"Атак обнаружено: {attack_count}\n"
        f"Заблокировано: {len(blocked_users)}\n"
        f"Доверенных: {len(whitelist)}\n"
        f"Максимальный Risk: {max_risk}/100\n"
        "\n"
        f"Последняя проверка: {now()}",
        reply_markup=main_keyboard(),
    )

    await callback.answer()


# ============================================================
# FASTAPI
# ============================================================

@app.get("/")
async def root():

    return {
        "service": "SENTINEL-X",
        "version": "2.0",
        "status": "online",
        "protection": protection_status(),
        "risk": calculate_risk(),
        "attacks": attack_count,
        "blocked": len(blocked_users),
    }


@app.get("/health")
async def health():

    return {
        "status": "ok",
        "service": "sentinel-x",
        "version": "2.0",
        "protection": protection_enabled,
        "emergency": emergency_mode,
        "panic": panic_mode,
        "risk": calculate_risk(),
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
    # Используем connect(), а не start().
    # Render не имеет интерактивного ввода.

    await user_client.connect()

    try:

        authorized = (
            await user_client.is_user_authorized()
        )

    except (
        AuthKeyUnregisteredError,
        SessionRevokedError,
    ):

        authorized = False

    if not authorized:

        raise RuntimeError(
            "SESSION_STRING недействительна "
            "или Telegram-сессия была отозвана"
        )

    me = await user_client.get_me()

    await admin_alert(
        "🟢 SENTINEL-X v2 запущен\n"
        "\n"
        f"Аккаунт: {me.first_name or 'Неизвестно'}\n"
        f"ID: {me.id}\n"
        "\n"
        "🛡 Защитные модули активны."
    )


# ============================================================
# BOT WORKER
# ============================================================

async def bot_worker():

    await init_database()

    await start_telegram()

    background_tasks.append(
        asyncio.create_task(
            check_sessions()
        )
    )

    background_tasks.append(
        asyncio.create_task(
            attack_cleanup()
        )
    )

    await dp.start_polling(
        bot
    )


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

    server = uvicorn.Server(
        config
    )

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