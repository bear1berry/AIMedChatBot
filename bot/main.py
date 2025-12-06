from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from services.engine import (
    Engine,
    MODE_CONFIGS,
    DEFAULT_MODE_KEY,
    describe_communication_style,
)

# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment")

DB_PATH = os.getenv("DB_PATH", "aimedbot.db")

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")
CRYPTO_DEFAULT_ASSET = os.getenv("CRYPTO_DEFAULT_ASSET", "USDT")

FREE_MESSAGES_LIMIT = int(os.getenv("FREE_MESSAGES_LIMIT", "20"))

ADMIN_USERNAMES = {
    u.strip().lower()
    for u in os.getenv("ADMIN_USERNAMES", "").replace(",", " ").split()
    if u.strip()
}

# есть ли вообще LLM (для ограничения бесплатных запросов)
LLM_AVAILABLE = bool(os.getenv("DEEPSEEK_API_KEY") or os.getenv("GROQ_API_KEY"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Тарифы (подписка)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    code: str
    title: str
    months: int
    price_usdt: float
    description: str


PLANS: Dict[str, Plan] = {
    "1m": Plan(
        code="1m",
        title="1 месяц доступа",
        months=1,
        price_usdt=7.99,
        description="Стартовый доступ к BlackBox GPT на 1 месяц.",
    ),
    "3m": Plan(
        code="3m",
        title="3 месяца доступа",
        months=3,
        price_usdt=26.99,
        description="Удобный пакет на 3 месяца со скидкой.",
    ),
    "12m": Plan(
        code="12m",
        title="12 месяцев доступа",
        months=12,
        price_usdt=82.99,
        description="Годовой доступ с максимальной выгодой.",
    ),
}


# ---------------------------------------------------------------------------
# База данных (users, referrals, invoices), messages создаётся здесь для Engine
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    cur = conn.cursor()

    # Пользователи
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            mode TEXT NOT NULL DEFAULT 'universal',
            free_used INTEGER NOT NULL DEFAULT 0,
            is_premium INTEGER NOT NULL DEFAULT 0,
            premium_until_ts INTEGER,
            created_at_ts INTEGER NOT NULL,
            updated_at_ts INTEGER NOT NULL
        )
        """
    )

    # style_profile_json для Style Engine
    try:
        cur.execute("PRAGMA table_info(users_v2)")
        cols = [row["name"] for row in cur.fetchall()]
        if "style_profile_json" not in cols:
            cur.execute("ALTER TABLE users_v2 ADD COLUMN style_profile_json TEXT")
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to ensure style_profile_json column: %r", e)

    # История сообщений (используется Engine)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at_ts INTEGER NOT NULL
        )
        """
    )

    # Реферальные связи
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_id INTEGER NOT NULL,
            invited_id INTEGER NOT NULL,
            created_at_ts INTEGER NOT NULL,
            UNIQUE(inviter_id, invited_id)
        )
        """
    )

    # Счета Crypto Pay
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER UNIQUE NOT NULL,
            telegram_id INTEGER NOT NULL,
            plan_code TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at_ts INTEGER NOT NULL,
            updated_at_ts INTEGER NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def get_or_create_user(
    telegram_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
) -> sqlite3.Row:
    now_ts = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users_v2 WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    if row:
        cur.execute(
            """
            UPDATE users_v2
            SET username = ?, first_name = ?, last_name = ?, updated_at_ts = ?
            WHERE telegram_id = ?
            """,
            (username, first_name, last_name, now_ts, telegram_id),
        )
    else:
        cur.execute(
            """
            INSERT INTO users_v2 (
                telegram_id,
                username,
                first_name,
                last_name,
                mode,
                free_used,
                is_premium,
                premium_until_ts,
                created_at_ts,
                updated_at_ts
            )
            VALUES (?, ?, ?, ?, ?, 0, 0, NULL, ?, ?)
            """,
            (telegram_id, username, first_name, last_name, DEFAULT_MODE_KEY, now_ts, now_ts),
        )

    conn.commit()
    cur.execute("SELECT * FROM users_v2 WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM users_v2 WHERE lower(username) = ?",
        (username.lower(),),
    )
    row = cur.fetchone()
    conn.close()
    return row


def user_is_premium(user_row: sqlite3.Row) -> bool:
    until = user_row["premium_until_ts"]
    if not until:
        return False
    try:
        return int(until) > int(time.time())
    except (ValueError, TypeError):
        return False


def grant_premium(telegram_id: int, months: int) -> None:
    extend_seconds = int(months * 30.4375 * 24 * 3600)
    now_ts = int(time.time())

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT premium_until_ts FROM users_v2 WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = cur.fetchone()
    current_until = int(row["premium_until_ts"]) if row and row["premium_until_ts"] else 0

    base = current_until if current_until > now_ts else now_ts
    new_until = base + extend_seconds

    cur.execute(
        """
        UPDATE users_v2
        SET is_premium = 1, premium_until_ts = ?, updated_at_ts = ?
        WHERE telegram_id = ?
        """,
        (new_until, now_ts, telegram_id),
    )
    conn.commit()
    conn.close()


def get_free_used(telegram_id: int) -> int:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT free_used FROM users_v2 WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row or row["free_used"] is None:
        return 0
    return int(row["free_used"])


def increment_free_used(telegram_id: int, delta: int = 1) -> int:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users_v2 SET free_used = free_used + ? WHERE telegram_id = ?",
        (delta, telegram_id),
    )
    conn.commit()

    cur.execute(
        "SELECT free_used FROM users_v2 WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row or row["free_used"] is None:
        return delta
    return int(row["free_used"])


def is_user_admin(username: Optional[str]) -> bool:
    return bool(username and username.lower() in ADMIN_USERNAMES)


def get_user_mode_from_row(user_row: sqlite3.Row) -> str:
    keys = {k for k in user_row.keys()}
    mode_val = user_row["mode"] if "mode" in keys else None
    if not mode_val or mode_val not in MODE_CONFIGS:
        return DEFAULT_MODE_KEY
    return str(mode_val)


def set_user_mode(telegram_id: int, mode_key: str) -> None:
    if mode_key not in MODE_CONFIGS:
        mode_key = DEFAULT_MODE_KEY
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users_v2 SET mode = ?, updated_at_ts = ? WHERE telegram_id = ?",
        (mode_key, int(time.time()), telegram_id),
    )
    conn.commit()
    conn.close()


def get_user_stats(telegram_id: int) -> Dict[str, Optional[int]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT created_at_ts, premium_until_ts, free_used
        FROM users_v2
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    )
    row = cur.fetchone()

    if row:
        created = int(row["created_at_ts"]) if row["created_at_ts"] else None
        premium_until = int(row["premium_until_ts"]) if row["premium_until_ts"] else None
        free_used = int(row["free_used"] or 0)
    else:
        created = premium_until = free_used = None

    cur.execute(
        "SELECT COUNT(*) AS cnt FROM messages WHERE telegram_id = ?",
        (telegram_id,),
    )
    msg_row = cur.fetchone()
    message_count = int(msg_row["cnt"] or 0) if msg_row else 0

    conn.close()
    return {
        "created_at_ts": created,
        "premium_until_ts": premium_until,
        "free_used": free_used,
        "message_count": message_count,
    }


# ---------------------------------------------------------------------------
# Рефералка
# ---------------------------------------------------------------------------

def register_referral(inviter_id: int, invited_id: int) -> None:
    if inviter_id == invited_id:
        return

    now_ts = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO referrals (inviter_id, invited_id, created_at_ts)
        VALUES (?, ?, ?)
        """,
        (inviter_id, invited_id, now_ts),
    )
    conn.commit()
    conn.close()


def get_referral_stats(inviter_id: int) -> Tuple[int, int]:
    now_ts = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT COUNT(DISTINCT invited_id) AS cnt FROM referrals WHERE inviter_id = ?",
        (inviter_id,),
    )
    row = cur.fetchone()
    invited = int(row["cnt"] or 0) if row else 0

    cur.execute(
        """
        SELECT COUNT(DISTINCT r.invited_id) AS cnt
        FROM referrals r
        JOIN users_v2 u ON u.telegram_id = r.invited_id
        WHERE r.inviter_id = ?
          AND u.premium_until_ts IS NOT NULL
          AND u.premium_until_ts > ?
        """,
        (inviter_id, now_ts),
    )
    row = cur.fetchone()
    premium = int(row["cnt"] or 0) if row else 0

    conn.close()
    return invited, premium


# ---------------------------------------------------------------------------
# Crypto Pay (инвойсы)
# ---------------------------------------------------------------------------

def save_invoice_record(invoice: Dict[str, Any], plan_code: str, telegram_id: int) -> None:
    invoice_id = int(invoice["invoice_id"])
    status = str(invoice.get("status") or "active").lower()
    now_ts = int(time.time())

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO invoices (
            invoice_id,
            telegram_id,
            plan_code,
            status,
            created_at_ts,
            updated_at_ts
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (invoice_id, telegram_id, plan_code, status, now_ts, now_ts),
    )

    if cur.rowcount == 0:
        cur.execute(
            """
            UPDATE invoices
            SET telegram_id = ?, plan_code = ?, status = ?, updated_at_ts = ?
            WHERE invoice_id = ?
            """,
            (telegram_id, plan_code, status, now_ts, invoice_id),
        )

    conn.commit()
    conn.close()


def get_active_invoices(limit: int = 100) -> List[sqlite3.Row]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT invoice_id, telegram_id, plan_code, status
        FROM invoices
        WHERE status IN ('active', 'created')
        ORDER BY created_at_ts ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def update_invoice_status(invoice_id: int, status: str) -> None:
    now_ts = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE invoices
        SET status = ?, updated_at_ts = ?
        WHERE invoice_id = ?
        """,
        (status, now_ts, invoice_id),
    )
    conn.commit()
    conn.close()


async def crypto_create_invoice(plan: Plan, telegram_id: int) -> Optional[str]:
    if not CRYPTO_PAY_API_TOKEN:
        return None

    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}
    payload = {
        "asset": CRYPTO_DEFAULT_ASSET,
        "amount": f"{plan.price_usdt:.2f}",
        "description": f"BlackBox GPT — {plan.title}",
        "expires_in": 3600,
        "payload": json.dumps({"telegram_id": telegram_id, "plan": plan.code}),
    }

    async with httpx.AsyncClient(base_url=CRYPTO_PAY_API_URL, timeout=15) as client:
        resp = await client.post("/createInvoice", headers=headers, json=payload)

    data = resp.json()
    if not data.get("ok"):
        logger.error("CryptoPay createInvoice error: %s", data)
        return None

    invoice = data["result"]

    try:
        save_invoice_record(invoice, plan.code, telegram_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to save invoice record: %r", e)

    pay_url = (
        invoice.get("bot_invoice_url")
        or invoice.get("mini_app_invoice_url")
        or invoice.get("pay_url")
    )
    return pay_url


async def crypto_get_invoices(invoice_ids: List[int]) -> List[Dict[str, Any]]:
    if not CRYPTO_PAY_API_TOKEN or not invoice_ids:
        return []

    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}
    payload = {"invoice_ids": invoice_ids}

    async with httpx.AsyncClient(base_url=CRYPTO_PAY_API_URL, timeout=15) as client:
        resp = await client.post("/getInvoices", headers=headers, json=payload)

    data = resp.json()
    if not data.get("ok"):
        logger.error("CryptoPay getInvoices error: %s", data)
        return []

    result = data.get("result") or []
    return result if isinstance(result, list) else []


# ---------------------------------------------------------------------------
# Доступ / лимиты
# ---------------------------------------------------------------------------

async def _ensure_user(message: Message) -> sqlite3.Row:
    from_user = message.from_user
    return get_or_create_user(
        telegram_id=from_user.id,
        username=from_user.username,
        first_name=from_user.first_name,
        last_name=from_user.last_name,
    )


async def _check_access(message: Message) -> Tuple[bool, sqlite3.Row]:
    user_row = await _ensure_user(message)
    username = message.from_user.username

    if is_user_admin(username):
        return True, user_row

    if user_is_premium(user_row):
        return True, user_row

    if not LLM_AVAILABLE:
        return True, user_row

    used = int(user_row["free_used"] or 0)
    if used >= FREE_MESSAGES_LIMIT:
        return False, user_row

    new_used = increment_free_used(user_row["telegram_id"])
    logger.info(
        "User %s used free message %s/%s",
        user_row["telegram_id"],
        new_used,
        FREE_MESSAGES_LIMIT,
    )
    return True, user_row


# ---------------------------------------------------------------------------
# UI: клавиатуры (таскбар)
# ---------------------------------------------------------------------------

BTN_MODES = "🧠 Режимы"
BTN_PROFILE = "👤 Профиль"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_REFERRALS = "👥 Рефералы"

BTN_MODE_UNIVERSAL = MODE_CONFIGS["universal"].button_text
BTN_MODE_MEDICAL = MODE_CONFIGS["medical"].button_text
BTN_MODE_MENTOR = MODE_CONFIGS["mentor"].button_text
BTN_MODE_BUSINESS = MODE_CONFIGS["business"].button_text
BTN_MODE_CREATIVE = MODE_CONFIGS["creative"].button_text

BTN_BACK = "⬅️ Назад"

BTN_PLAN_1M = "💎 1 месяц"
BTN_PLAN_3M = "💎 3 месяца"
BTN_PLAN_12M = "💎 12 месяцев"

PLAN_BUTTON_TO_CODE: Dict[str, str] = {
    BTN_PLAN_1M: "1m",
    BTN_PLAN_3M: "3m",
    BTN_PLAN_12M: "12m",
}

MENU_TEXTS = {
    BTN_MODES,
    BTN_PROFILE,
    BTN_SUBSCRIPTION,
    BTN_REFERRALS,
    BTN_MODE_UNIVERSAL,
    BTN_MODE_MEDICAL,
    BTN_MODE_MENTOR,
    BTN_MODE_BUSINESS,
    BTN_MODE_CREATIVE,
    BTN_BACK,
    BTN_PLAN_1M,
    BTN_PLAN_3M,
    BTN_PLAN_12M,
}


def main_taskbar() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODES), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_SUBSCRIPTION), KeyboardButton(text=BTN_REFERRALS)],
        ],
        resize_keyboard=True,
    )


def modes_taskbar() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODE_UNIVERSAL), KeyboardButton(text=BTN_MODE_MEDICAL)],
            [KeyboardButton(text=BTN_MODE_MENTOR), KeyboardButton(text=BTN_MODE_BUSINESS)],
            [KeyboardButton(text=BTN_MODE_CREATIVE), KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def subscription_taskbar() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_PLAN_1M), KeyboardButton(text=BTN_PLAN_3M)],
            [KeyboardButton(text=BTN_PLAN_12M), KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


# ---------------------------------------------------------------------------
# Живое печатание 2.0 (стриминг) — чисто Telegram-транспорт
# ---------------------------------------------------------------------------

STREAM_CHUNK_SIZE = 80
STREAM_MAX_STEPS = 40
STREAM_DELAY_SECONDS = 0.12


def _chunk_text_for_streaming(text: str) -> List[str]:
    text = text.strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    raw_chunks: List[str] = []

    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            continue

        current = ""
        for w in words:
            if len(current) + len(w) + (1 if current else 0) <= STREAM_CHUNK_SIZE:
                current = f"{current} {w}".strip()
            else:
                if current:
                    raw_chunks.append(current)
                current = w
        if current:
            raw_chunks.append(current)

        raw_chunks.append("\n")

    if raw_chunks and raw_chunks[-1] == "\n":
        raw_chunks.pop()

    if len(raw_chunks) <= STREAM_MAX_STEPS:
        return raw_chunks

    step = math.ceil(len(raw_chunks) / STREAM_MAX_STEPS)
    chunks: List[str] = []
    for i in range(0, len(raw_chunks), step):
        chunks.append(" ".join(raw_chunks[i : i + step]).replace(" \n ", "\n"))
    return chunks


async def stream_reply_text(message: Message, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return

    chunks = _chunk_text_for_streaming(text)
    if len(chunks) <= 1:
        await message.answer(text)
        return

    bot = message.bot

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    current = chunks[0]
    sent = await message.answer(current)

    for chunk in chunks[1:]:
        await asyncio.sleep(STREAM_DELAY_SECONDS)
        current = f"{current} {chunk}".strip()
        try:
            await bot.edit_message_text(
                current,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                parse_mode=None,
            )
            await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        except Exception as e:  # noqa: BLE001
            logger.exception("Streaming edit error: %r", e)
            if current != text:
                await message.answer(text, parse_mode=None)
            break


# ---------------------------------------------------------------------------
# Router + Engine
# ---------------------------------------------------------------------------

router = Router()
engine = Engine()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    inviter_id: Optional[int] = None
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1].strip()
            if arg.startswith("ref_"):
                try:
                    inviter_id = int(arg[4:])
                except ValueError:
                    inviter_id = None

    user_row = await _ensure_user(message)

    if inviter_id:
        register_referral(inviter_id, int(user_row["telegram_id"]))

    mode_key = get_user_mode_from_row(user_row)
    mode_cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    is_premium = user_is_premium(user_row)
    used = int(user_row["free_used"] or 0)
    left = max(FREE_MESSAGES_LIMIT - used, 0)

    if is_premium:
        status_line = (
            "Премиум-доступ <b>активен</b>: общение без лимитов и приоритетная скорость ответов."
        )
    else:
        status_line = (
            f"Сейчас у тебя базовый доступ. Доступно ≈ <b>{left}</b> бесплатных сообщений "
            f"из {FREE_MESSAGES_LIMIT}, дальше — через подписку."
        )

    name = message.from_user.first_name or "друг"

    text = (
        f"<b>Привет, {name}!</b>\n\n"
        "<b>BlackBox GPT</b> — универсальный ИИ-ассистент премиум-класса.\n"
        "Минимализм во всём: только диалог и нижний таскбар.\n\n"
        "<b>Как работать:</b>\n"
        "• просто задай первый вопрос — от медицины и бизнеса до личного развития;\n"
        "• или выбери режим внизу, если нужен особый фокус.\n\n"
        f"{status_line}\n\n"
        f"<b>Текущий режим:</b> {mode_cfg.title} — {mode_cfg.short_label}.\n\n"
        "Пиши, чем тебе помочь прямо сейчас."
    )

    await message.answer(text, reply_markup=main_taskbar())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Что умеет BlackBox GPT</b>\n\n"
        "• разбирать сложные ситуации и помогать принять решение;\n"
        "• собирать планы и чек-листы под твои задачи;\n"
        "• помогать с текстами, идеями, креативом и кодом;\n"
        "• аккуратно давать справочную информацию по медицине (но не ставить диагнозы).\n\n"
        "Выбирай режим внизу или просто пиши запрос — дальше можно общаться как с живым умным собеседником."
    )
    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Режимы --------------------------

@router.message(F.text == BTN_MODES)
async def show_modes_menu(message: Message) -> None:
    text = (
        "<b>Режимы BlackBox GPT</b>\n\n"
        "Выбери, в каком фокусе сейчас нужен ассистент:\n"
        f"• {MODE_CONFIGS['universal'].short_label};\n"
        f"• {MODE_CONFIGS['medical'].short_label};\n"
        f"• {MODE_CONFIGS['mentor'].short_label};\n"
        f"• {MODE_CONFIGS['business'].short_label};\n"
        f"• {MODE_CONFIGS['creative'].short_label}.\n\n"
        "Нажми нужный режим на таскбаре ниже — я сразу подстрою стиль ответов."
    )
    await message.answer(text, reply_markup=modes_taskbar())


async def _set_mode_and_confirm(message: Message, mode_key: str) -> None:
    set_user_mode(message.from_user.id, mode_key)
    cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    text = (
        f"<b>Режим установлен:</b> {cfg.title}.\n\n"
        f"{cfg.description}\n\n"
        "Можешь сразу писать следующий запрос — я отвечу в этом режиме."
    )
    await message.answer(text, reply_markup=main_taskbar())


@router.message(F.text == BTN_MODE_UNIVERSAL)
async def set_mode_universal(message: Message) -> None:
    await _set_mode_and_confirm(message, "universal")


@router.message(F.text == BTN_MODE_MEDICAL)
async def set_mode_medical(message: Message) -> None:
    await _set_mode_and_confirm(message, "medical")


@router.message(F.text == BTN_MODE_MENTOR)
async def set_mode_mentor(message: Message) -> None:
    await _set_mode_and_confirm(message, "mentor")


@router.message(F.text == BTN_MODE_BUSINESS)
async def set_mode_business(message: Message) -> None:
    await _set_mode_and_confirm(message, "business")


@router.message(F.text == BTN_MODE_CREATIVE)
async def set_mode_creative(message: Message) -> None:
    await _set_mode_and_confirm(message, "creative")


# -------------------------- Профиль --------------------------

@router.message(F.text == BTN_PROFILE)
async def show_profile(message: Message) -> None:
    user_row = await _ensure_user(message)
    telegram_id = int(user_row["telegram_id"])

    stats = get_user_stats(telegram_id)
    style_desc = describe_communication_style(telegram_id)

    mode_key = get_user_mode_from_row(user_row)
    mode_cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    is_premium = user_is_premium(user_row)
    used = stats["free_used"] or 0
    remaining = max(FREE_MESSAGES_LIMIT - used, 0)

    if is_premium:
        access_status = (
            "Премиум-доступ <b>активен</b> — можешь общаться без лимитов и очередей."
        )
    else:
        access_status = (
            "Базовый доступ.\n"
            f"Использовано <b>{used}</b> бесплатных сообщений из <b>{FREE_MESSAGES_LIMIT}</b>. "
            f"Осталось ≈ <b>{remaining}</b>."
        )

    created_line = ""
    if stats["created_at_ts"]:
        created_dt = time.strftime("%d.%m.%Y", time.localtime(stats["created_at_ts"]))
        created_line = f"\n<b>С BlackBox GPT с:</b> {created_dt}"

    messages_line = ""
    if stats["message_count"]:
        messages_line = f"\n<b>Сообщений в диалоге:</b> {stats['message_count']}"

    username = message.from_user.username or "без username"

    text = (
        "<b>Профиль BlackBox GPT</b>\n\n"
        f"<b>Аккаунт:</b> @{username}\n"
        f"<b>ID:</b> <code>{telegram_id}</code>\n"
        f"<b>Текущий режим:</b> {mode_cfg.title} — {mode_cfg.short_label}."
        f"{created_line}"
        f"{messages_line}\n\n"
        "<b>Статус доступа</b>\n"
        f"{access_status}\n\n"
        "<b>Как я тебя чувствую:</b>\n"
        f"• {style_desc}\n"
        "• бот подстраивает тон, длину и формат ответов под твой стиль общения.\n\n"
        "<i>Дальше сюда добавим цели, проекты и привычки — профиль станет "
        "личной панелью управления твоим ИИ-ассистентом.</i>"
    )

    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Рефералы --------------------------

@router.message(F.text == BTN_REFERRALS)
async def show_referrals(message: Message) -> None:
    user_row = await _ensure_user(message)
    telegram_id = int(user_row["telegram_id"])

    invited, premium = get_referral_stats(telegram_id)

    me = await message.bot.get_me()
    bot_username = me.username
    ref_link = f"https://t.me/{bot_username}?start=ref_{telegram_id}"

    text = (
        "<b>Реферальная система BlackBox GPT</b>\n\n"
        "У тебя есть персональная ссылка. Все, кто запускают бота по ней, "
        "закрепляются за твоим аккаунтом.\n\n"
        "<b>Твоя ссылка:</b>\n"
        f'<a href="{ref_link}">{ref_link}</a>\n\n'
        "<i>Нажми и удерживай, чтобы скопировать или сразу отправить друзьям.</i>\n\n"
        "<b>Твоя статистика:</b>\n"
        f"• приглашено пользователей: <b>{invited}</b>\n"
        f"• из них с активным премиумом: <b>{premium}</b>\n\n"
        "<i>В следующих обновлениях сюда добавим конкретные бонусы за приглашения: "
        "дополнительные запросы, закрытые режимы и особые инструменты.</i>"
    )

    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Подписка --------------------------

async def _subscription_overview_text(user_row: sqlite3.Row) -> str:
    is_premium = user_is_premium(user_row)
    used = int(user_row["free_used"] or 0)
    left = max(FREE_MESSAGES_LIMIT - used, 0)

    if is_premium:
        header = (
            "<b>Подписка BlackBox GPT</b>\n\n"
            "Премиум уже активен — продлить доступ можно в любой момент.\n\n"
        )
    else:
        header = (
            "<b>Подписка BlackBox GPT</b>\n\n"
            f"Сейчас у тебя базовый доступ и ≈ <b>{left}</b> бесплатных сообщений.\n"
            "Подписка снимает ограничения и даёт приоритетную скорость обработки запросов.\n\n"
        )

    lines = [header, "<b>Тарифы (оплата в USDT через Crypto Bot):</b>"]
    for code in ("1m", "3m", "12m"):
        plan = PLANS[code]
        lines.append(f"• {plan.title} — {plan.price_usdt:.2f} USDT")

    lines.append(
        "\nВыбери тариф на таскбаре ниже — я создам персональную ссылку на оплату в Crypto Bot."
    )
    return "\n".join(lines)


@router.message(Command("subscription"))
@router.message(F.text == BTN_SUBSCRIPTION)
async def show_subscription(message: Message) -> None:
    user_row = await _ensure_user(message)
    text = await _subscription_overview_text(user_row)
    await message.answer(text, reply_markup=subscription_taskbar())


@router.message(F.text.in_(list(PLAN_BUTTON_TO_CODE.keys())))
async def handle_subscription_plan(message: Message) -> None:
    plan_code = PLAN_BUTTON_TO_CODE[message.text]
    plan = PLANS.get(plan_code)
    if not plan:
        await message.answer(
            "Не удалось определить тариф. Попробуй выбрать ещё раз.",
            reply_markup=subscription_taskbar(),
        )
        return

    if not CRYPTO_PAY_API_TOKEN:
        await message.answer(
            "Платёжный модуль ещё не настроен. Свяжись с админом, если хочешь протестировать оплату.",
            reply_markup=main_taskbar(),
        )
        return

    pay_url = await crypto_create_invoice(plan, message.from_user.id)
    if not pay_url:
        await message.answer(
            "Не удалось создать счёт. Попробуй позже.",
            reply_markup=subscription_taskbar(),
        )
        return

    text = (
        "<b>Оформление подписки BlackBox GPT</b>\n\n"
        f"<b>План:</b> {plan.title}\n"
        f"<b>Сумма:</b> {plan.price_usdt:.2f} USDT\n\n"
        "Ссылка на оплату через Crypto Bot:\n"
        f"{pay_url}\n\n"
        "Как только платёж пройдёт, бот автоматически активирует премиум-доступ "
        "и отправит тебе уведомление."
    )
    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Назад --------------------------

@router.message(F.text == BTN_BACK)
async def handle_back(message: Message) -> None:
    text = (
        "Возвращаю на главный экран.\n"
        "Снизу снова универсальный таскбар: режимы, профиль, подписка и рефералы."
    )
    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Админ-команда --------------------------

@router.message(Command("grant_premium"))
async def cmd_grant_premium(message: Message) -> None:
    if not is_user_admin(message.from_user.username):
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(
            "Использование:\n"
            "/grant_premium <telegram_id|@username> <месяцев>\n\n"
            "Примеры:\n"
            "/grant_premium 123456789 1\n"
            "/grant_premium @nickname 3"
        )
        return

    target = parts[1]
    try:
        months = int(parts[2])
    except ValueError:
        await message.reply("Количество месяцев должно быть целым числом.")
        return

    if months <= 0:
        await message.reply("Количество месяцев должно быть положительным.")
        return

    if target.startswith("@"):
        user_row = get_user_by_username(target[1:])
        if not user_row:
            await message.reply("Пользователь с таким username не найден в базе.")
            return
        telegram_id = int(user_row["telegram_id"])
    else:
        try:
            telegram_id = int(target)
        except ValueError:
            await message.reply("Некорректный telegram_id.")
            return

    grant_premium(telegram_id, months)
    await message.reply(
        f"Премиум на {months} мес. выдан пользователю <code>{telegram_id}</code>.",
        parse_mode=ParseMode.HTML,
    )


# -------------------------- Основной диалог --------------------------

@router.message(F.chat.type == ChatType.PRIVATE)
async def handle_private_chat(message: Message) -> None:
    if not message.text:
        return

    text = message.text.strip()
    if not text:
        return

    if text in MENU_TEXTS:
        return

    if text.startswith("/"):
        return

    allowed, user_row = await _check_access(message)
    if not allowed:
        used = get_free_used(message.from_user.id)
        msg = (
            "⚠️ Лимит бесплатных сообщений исчерпан.\n\n"
            f"Ты уже использовал {used} из {FREE_MESSAGES_LIMIT}.\n\n"
            "Чтобы продолжить общение без ограничений, открой раздел «💎 Подписка» "
            "и оформи премиум-доступ."
        )
        await message.answer(msg, reply_markup=main_taskbar())
        return

    telegram_id = message.from_user.id
    mode_key = get_user_mode_from_row(user_row)

    engine_answer = await engine.handle_message(
        telegram_id=telegram_id,
        text=text,
        mode_key=mode_key,
    )

    if not engine_answer.text:
        return

    if engine_answer.use_stream:
        await stream_reply_text(message, engine_answer.text)
    else:
        await message.answer(engine_answer.text)


# ---------------------------------------------------------------------------
# Фоновый воркер для счетов
# ---------------------------------------------------------------------------

async def invoice_watcher(bot: Bot) -> None:
    if not CRYPTO_PAY_API_TOKEN:
        return

    logger.info("Invoice watcher started")

    while True:
        try:
            active = get_active_invoices(limit=100)
            if not active:
                await asyncio.sleep(30)
                continue

            invoice_ids = [int(r["invoice_id"]) for r in active]
            remote_list = await crypto_get_invoices(invoice_ids)
            remote_by_id = {int(inv["invoice_id"]): inv for inv in remote_list}

            for row in active:
                iid = int(row["invoice_id"])
                local_status = str(row["status"])
                inv = remote_by_id.get(iid)
                if not inv:
                    continue

                status_remote = str(inv.get("status") or "").lower()
                if status_remote == local_status:
                    continue

                update_invoice_status(iid, status_remote)

                tg_id = int(row["telegram_id"])
                plan_code = row["plan_code"]
                plan = PLANS.get(plan_code)

                if status_remote == "paid":
                    if plan:
                        grant_premium(tg_id, plan.months)

                    try:
                        await bot.send_message(
                            tg_id,
                            (
                                "<b>Подписка активирована</b>\n\n"
                                f"Платёж за план «{plan.title if plan else 'подписка'}» "
                                "успешно получен. Премиум-доступ уже включён."
                            ),
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.exception(
                            "Failed to send premium activation message: %r", e
                        )

                elif status_remote == "expired":
                    try:
                        await bot.send_message(
                            tg_id,
                            (
                                "Счёт на оплату подписки истёк.\n"
                                "Если хочешь продолжить — открой раздел «💎 Подписка» "
                                "и создай новый счёт."
                            ),
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.exception(
                            "Failed to send invoice expired message: %r", e
                        )

        except Exception as e:  # noqa: BLE001
            logger.exception("Invoice watcher loop error: %r", e)

        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Запуск бота
# ---------------------------------------------------------------------------

async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="help", description="Что умеет бот"),
        BotCommand(command="subscription", description="Оформить подписку"),
    ]
    await bot.set_my_commands(commands)


async def main() -> None:
    init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    await set_bot_commands(bot)

    if CRYPTO_PAY_API_TOKEN:
        asyncio.create_task(invoice_watcher(bot))

    logger.info("Starting BlackBox GPT bot polling…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
