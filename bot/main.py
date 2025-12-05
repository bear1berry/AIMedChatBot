"""
BlackBox GPT Bot main module.

Фичи:
- Режимы ассистента.
- Лимит бесплатных сообщений + Premium по подписке.
- Память / личное досье на пользователя.
- Реферальная система с бонусными днями Premium.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")

FREE_MESSAGES_LIMIT = int(os.getenv("FREE_MESSAGES_LIMIT", "20"))
ADMIN_USERNAMES = {
    u.strip().lower()
    for u in os.getenv("ADMIN_USERNAMES", "").replace(",", " ").split()
    if u.strip()
}

DB_PATH = os.getenv("SUBSCRIPTION_DB_PATH", "subscription.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")

if not (DEEPSEEK_API_KEY or GROQ_API_KEY):
    # можно работать, но модель не дернется
    logging.warning(
        "LLM keys are not configured. Set DEEPSEEK_API_KEY or GROQ_API_KEY in .env"
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# База: bb_users / bb_usage_stats / bb_subscriptions / bb_invoices / bb_referrals
# ---------------------------------------------------------------------------


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Создаем наши таблицы, не трогая старые (названия bb_*)."""
    with _get_conn() as conn:
        cur = conn.cursor()

        # Пользователи + режим + память
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bb_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'universal',
                memory TEXT NOT NULL DEFAULT '',
                created_at_ts INTEGER NOT NULL,
                updated_at_ts INTEGER NOT NULL
            )
            """
        )

        # Статистика использования
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bb_usage_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                messages_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, date),
                FOREIGN KEY (user_id) REFERENCES bb_users(id) ON DELETE CASCADE
            )
            """
        )

        # Подписки
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bb_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan_code TEXT NOT NULL,
                start_ts INTEGER NOT NULL,
                end_ts INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES bb_users(id) ON DELETE CASCADE
            )
            """
        )

        # Счета CryptoBot
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bb_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                invoice_id TEXT NOT NULL UNIQUE,
                plan_code TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at_ts INTEGER NOT NULL,
                paid_at_ts INTEGER
            )
            """
        )

        # Реферальная программа
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bb_referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_telegram_id INTEGER NOT NULL,
                referred_telegram_id INTEGER NOT NULL,
                created_at_ts INTEGER NOT NULL,
                UNIQUE(referred_telegram_id),
                CHECK(referrer_telegram_id != referred_telegram_id)
            )
            """
        )

    logger.info("Database initialized at %s", DB_PATH)


def _row_to_user(row: sqlite3.Row) -> Dict:
    return {
        "id": row["id"],
        "telegram_id": row["telegram_id"],
        "username": row["username"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "is_admin": bool(row["is_admin"]),
        "mode": row["mode"],
        "memory": row["memory"] or "",
        "created_at_ts": row["created_at_ts"],
        "updated_at_ts": row["updated_at_ts"],
    }


def get_or_create_user(
    telegram_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
) -> Dict:
    """
    Создаёт пользователя при первом заходе, либо обновляет данные.
    В словаре результата есть флаг 'is_new' (только что создан).
    """
    now_ts = int(time.time())
    is_admin = (username or "").lower() in ADMIN_USERNAMES

    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bb_users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        if row:
            cur.execute(
                """
                UPDATE bb_users
                SET username = ?, first_name = ?, last_name = ?, is_admin = ?, updated_at_ts = ?
                WHERE telegram_id = ?
                """,
                (username, first_name, last_name, int(is_admin), now_ts, telegram_id),
            )
            data = _row_to_user(row)
            data["is_new"] = False
            data["just_created"] = False
            return data

        cur.execute(
            """
            INSERT INTO bb_users (
                telegram_id, username, first_name, last_name,
                is_admin, mode, memory, created_at_ts, updated_at_ts
            )
            VALUES (?, ?, ?, ?, ?, 'universal', '', ?, ?)
            """,
            (telegram_id, username, first_name, last_name, int(is_admin), now_ts, now_ts),
        )
        user_id = cur.lastrowid
        cur.execute("SELECT * FROM bb_users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        data = _row_to_user(row)
        data["is_new"] = True
        data["just_created"] = True
        return data


def get_user(telegram_id: int) -> Optional[Dict]:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bb_users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        if not row:
            return None
        data = _row_to_user(row)
        data["is_new"] = False
        data["just_created"] = False
        return data


def set_user_mode(telegram_id: int, mode: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE bb_users SET mode = ?, updated_at_ts = ? WHERE telegram_id = ?",
            (mode, int(time.time()), telegram_id),
        )


def get_user_mode(telegram_id: int) -> str:
    user = get_user(telegram_id)
    return user["mode"] if user else "universal"


def get_user_memory(telegram_id: int) -> str:
    user = get_user(telegram_id)
    return user["memory"] if user else ""


def set_user_memory(telegram_id: int, memory_text: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE bb_users SET memory = ?, updated_at_ts = ? WHERE telegram_id = ?",
            (memory_text.strip(), int(time.time()), telegram_id),
        )


def append_user_memory(telegram_id: int, new_fact: str) -> str:
    current = get_user_memory(telegram_id)
    new_fact = new_fact.strip()
    if not new_fact:
        return current

    if current:
        updated = current + "\n• " + new_fact
    else:
        updated = "• " + new_fact

    set_user_memory(telegram_id, updated)
    return updated


def clear_user_memory(telegram_id: int) -> None:
    set_user_memory(telegram_id, "")


def _get_user_id(conn: sqlite3.Connection, telegram_id: int) -> Optional[int]:
    cur = conn.cursor()
    cur.execute("SELECT id FROM bb_users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    return int(row["id"]) if row else None


def get_usage_today(telegram_id: int, date_str: str) -> int:
    with _get_conn() as conn:
        user_id = _get_user_id(conn, telegram_id)
        if not user_id:
            return 0
        cur = conn.cursor()
        cur.execute(
            "SELECT messages_count FROM bb_usage_stats WHERE user_id = ? AND date = ?",
            (user_id, date_str),
        )
        row = cur.fetchone()
        return int(row["messages_count"]) if row else 0


def increment_usage(telegram_id: int, date_str: str) -> int:
    with _get_conn() as conn:
        user_id = _get_user_id(conn, telegram_id)
        if not user_id:
            return 0

        cur = conn.cursor()
        cur.execute(
            "SELECT id, messages_count FROM bb_usage_stats WHERE user_id = ? AND date = ?",
            (user_id, date_str),
        )
        row = cur.fetchone()

        if row:
            new_count = int(row["messages_count"]) + 1
            cur.execute(
                "UPDATE bb_usage_stats SET messages_count = ? WHERE id = ?",
                (new_count, row["id"]),
            )
        else:
            new_count = 1
            cur.execute(
                """
                INSERT INTO bb_usage_stats (user_id, date, messages_count)
                VALUES (?, ?, ?)
                """,
                (user_id, date_str, new_count),
            )

        return new_count


def add_subscription(telegram_id: int, plan_code: str, duration_days: int) -> None:
    now_ts = int(time.time())
    with _get_conn() as conn:
        user_id = _get_user_id(conn, telegram_id)
        if not user_id:
            return

        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM bb_subscriptions
            WHERE user_id = ? AND end_ts > ?
            ORDER BY end_ts DESC
            LIMIT 1
            """,
            (user_id, now_ts),
        )
        row = cur.fetchone()

        if row:
            new_end = int(row["end_ts"]) + duration_days * 86400
            cur.execute(
                "UPDATE bb_subscriptions SET end_ts = ?, plan_code = ? WHERE id = ?",
                (new_end, plan_code, row["id"]),
            )
        else:
            end_ts = now_ts + duration_days * 86400
            cur.execute(
                """
                INSERT INTO bb_subscriptions (user_id, plan_code, start_ts, end_ts)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, plan_code, now_ts, end_ts),
            )


def get_active_subscription(telegram_id: int) -> Optional[Dict]:
    now_ts = int(time.time())
    with _get_conn() as conn:
        user_id = _get_user_id(conn, telegram_id)
        if not user_id:
            return None

        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM bb_subscriptions
            WHERE user_id = ? AND end_ts > ?
            ORDER BY end_ts DESC
            LIMIT 1
            """,
            (user_id, now_ts),
        )
        row = cur.fetchone()
        if not row:
            return None

        return {
            "id": row["id"],
            "plan_code": row["plan_code"],
            "start_ts": row["start_ts"],
            "end_ts": row["end_ts"],
        }


def is_premium(telegram_id: int) -> bool:
    return get_active_subscription(telegram_id) is not None


def create_invoice_record(
    telegram_id: int, invoice_id: str, plan_code: str, status: str = "pending"
) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO bb_invoices (
                telegram_id, invoice_id, plan_code, status, created_at_ts
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_id, invoice_id, plan_code, status, int(time.time())),
        )


def mark_invoice_paid(invoice_id: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE bb_invoices
            SET status = 'paid', paid_at_ts = ?
            WHERE invoice_id = ?
            """,
            (int(time.time()), invoice_id),
        )


def get_last_pending_invoice(telegram_id: int) -> Optional[Dict]:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM bb_invoices
            WHERE telegram_id = ? AND status = 'pending'
            ORDER BY created_at_ts DESC
            LIMIT 1
            """,
            (telegram_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        return {
            "invoice_id": row["invoice_id"],
            "plan_code": row["plan_code"],
            "status": row["status"],
        }


# ----------------------- Реферальная программа -----------------------------


def register_referral(referrer_tid: int, referred_tid: int) -> bool:
    """
    Регистрирует рефералку.
    Возвращает True, если это первая регистрация для данного приглашённого.
    """
    if referrer_tid == referred_tid:
        return False

    now_ts = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        # Уже кто-то привёл этого юзера?
        cur.execute(
            "SELECT 1 FROM bb_referrals WHERE referred_telegram_id = ?",
            (referred_tid,),
        )
        if cur.fetchone():
            return False

        cur.execute(
            """
            INSERT INTO bb_referrals (
                referrer_telegram_id, referred_telegram_id, created_at_ts
            )
            VALUES (?, ?, ?)
            """,
            (referrer_tid, referred_tid, now_ts),
        )
    return True


def get_referral_stats(referrer_tid: int) -> int:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM bb_referrals
            WHERE referrer_telegram_id = ?
            """,
            (referrer_tid,),
        )
        row = cur.fetchone()
        return int(row["c"] if row else 0)


def encode_ref_code(telegram_id: int) -> str:
    """Короткий код по telegram_id (base36)."""
    n = int(telegram_id)
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    res = []
    while n > 0:
        n, rem = divmod(n, 36)
        res.append(digits[rem])
    return "".join(reversed(res))


def decode_ref_code(code: str) -> Optional[int]:
    try:
        return int(code, 36)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


async def _call_deepseek(system_prompt: str, user_text: str) -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.7,
        "max_tokens": 1024,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.deepseek.com/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        logger.error("Invalid DeepSeek response: %s", data)
        raise RuntimeError(f"DeepSeek API error: {e}") from e


async def _call_groq(system_prompt: str, user_text: str) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.1-70b-versatile",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.7,
        "max_tokens": 1024,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        logger.error("Invalid Groq response: %s", data)
        raise RuntimeError(f"Groq API error: {e}") from e


async def generate_ai_reply(telegram_id: int, user_text: str) -> str:
    mode_code = get_user_mode(telegram_id)
    memory_text = get_user_memory(telegram_id)

    mode_hint = {
        "universal": (
            "Отвечай универсально: баланс глубины и скорости. "
            "Можно смешивать факты, стратегии и личные советы."
        ),
        "deep_dive": (
            "Отвечай развернуто и глубоко: разбор по шагам, примеры, риски и альтернативы."
        ),
        "focus": (
            "Отвечай максимально прагматично: приоритеты, чек-листы, конкретные шаги."
        ),
        "creative": (
            "Отвечай креативно: идеи, необычные ракурсы, метафоры, но без потери смысла."
        ),
        "mentor": (
            "Отвечай как ментор: поддержка, честное зеркало, иногда жестко, но по делу."
        ),
    }.get(mode_code, "Отвечай по делу и понятно для умного собеседника.")

    system_parts = [
        "Ты — BlackBox GPT, универсальный персональный ассистент в Telegram.",
        "Помогаешь в задачах, работе, деньгах, здоровье, отношениях, идеях и т.д.",
        "Не ставь медицинских диагнозов и не заменяй врача. "
        "Можно делиться общими сведениями, возможными причинами и рекомендуемыми действиями, "
        "но всегда напоминай про очную консультацию при серьёзных состояниях.",
        f"Текущий стиль работы: {mode_hint}",
    ]

    if memory_text.strip():
        system_parts.append(
            "Вот важная информация о пользователе. Не повторяй её дословно, "
            "а используй как контекст:"
        )
        system_parts.append(memory_text)

    system_prompt = "\n\n".join(system_parts)

    if DEEPSEEK_API_KEY:
        return await _call_deepseek(system_prompt, user_text)
    if GROQ_API_KEY:
        return await _call_groq(system_prompt, user_text)

    raise RuntimeError("Нет настроенных ключей LLM")


# ---------------------------------------------------------------------------
# CryptoBot
# ---------------------------------------------------------------------------


async def create_crypto_invoice(
    amount_usdt: float,
    description: str,
    payload: Optional[str] = None,
    asset: str = "USDT",
) -> Dict:
    if not CRYPTO_PAY_API_TOKEN:
        raise RuntimeError("CRYPTO_PAY_API_TOKEN is not configured")

    url = CRYPTO_PAY_API_URL.rstrip("/") + "/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}
    data = {
        "asset": asset,
        "amount": str(amount_usdt),
        "description": description,
    }
    if payload:
        data["payload"] = payload

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data=data, headers=headers)
        resp.raise_for_status()
        body = resp.json()

    if not body.get("ok"):
        raise RuntimeError(f"CryptoBot error: {body}")

    return body["result"]


async def get_crypto_invoice(invoice_id: str) -> Dict:
    if not CRYPTO_PAY_API_TOKEN:
        raise RuntimeError("CRYPTO_PAY_API_TOKEN is not configured")

    url = CRYPTO_PAY_API_URL.rstrip("/") + "/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}
    data = {"invoice_ids": invoice_id}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, data=data, headers=headers)
        resp.raise_for_status()
        body = resp.json()

    if not body.get("ok"):
        raise RuntimeError(f"CryptoBot error: {body}")

    items = body.get("result", {}).get("items", [])
    if not items:
        raise RuntimeError("Invoice not found")

    return items[0]


# ---------------------------------------------------------------------------
# Режимы ассистента
# ---------------------------------------------------------------------------


@dataclass
class Mode:
    code: str
    title: str
    description: str
    emoji: str


MODES: Dict[str, Mode] = {
    "universal": Mode(
        code="universal",
        title="Универсальный ✓",
        description="Баланс глубины и скорости. Можно спрашивать обо всём — от жизни до кода.",
        emoji="🧠",
    ),
    "deep_dive": Mode(
        code="deep_dive",
        title="Глубокий разбор",
        description="Детальный анализ ситуаций, систем и стратегий.",
        emoji="🧩",
    ),
    "focus": Mode(
        code="focus",
        title="Фокус / Задачи",
        description="Приоритеты, структура, план действий. Убираем шум.",
        emoji="🎯",
    ),
    "creative": Mode(
        code="creative",
        title="Креатив / Идеи",
        description="Брейншторм для контента, проектов, подарков, решений.",
        emoji="🔥",
    ),
    "mentor": Mode(
        code="mentor",
        title="Ментор / Мотивация",
        description="Поддержка, честность, мотивационные речи и разбор установок.",
        emoji="🧭",
    ),
}


def format_modes_list(active_code: str) -> str:
    lines: list[str] = []
    for mode in MODES.values():
        active = mode.code == active_code
        lines.append(
            f"{mode.emoji} <b>{mode.title}</b> {'(активен)' if active else ''}".strip()
        )
        lines.append(f"   {mode.description}")
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Клавиатуры (нижний таскбар)
# ---------------------------------------------------------------------------

BTN_NEW = "💡 Новый запрос"
BTN_MODES = "🎛 Режим"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_MEMORY = "🧠 Память / профиль"
BTN_REFERRAL = "👥 Рефералы"
BTN_HELP = "❔ Помощь"

BTN_MEMORY_ADD = "➕ Добавить факт"
BTN_MEMORY_SHOW = "📋 Мое досье"
BTN_MEMORY_CLEAR = "🧹 Очистить память"
BTN_BACK = "⬅️ Назад"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_MODES)],
        [KeyboardButton(text=BTN_MEMORY), KeyboardButton(text=BTN_SUBSCRIPTION)],
        [KeyboardButton(text=BTN_REFERRAL), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)

MEMORY_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MEMORY_ADD), KeyboardButton(text=BTN_MEMORY_SHOW)],
        [KeyboardButton(text=BTN_MEMORY_CLEAR)],
        [KeyboardButton(text=BTN_BACK)],
    ],
    resize_keyboard=True,
)

MODES_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🧠 Универсальный"),
            KeyboardButton(text="🧩 Глубокий разбор"),
        ],
        [
            KeyboardButton(text="🎯 Фокус / Задачи"),
            KeyboardButton(text="🔥 Креатив / Идеи"),
        ],
        [
            KeyboardButton(text="🧭 Ментор / Мотивация"),
            KeyboardButton(text=BTN_BACK),
        ],
    ],
    resize_keyboard=True,
)

SUBSCRIPTION_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="1 месяц — 5 USDT")],
        [KeyboardButton(text="3 месяца — 12 USDT")],
        [KeyboardButton(text="12 месяцев — 60 USDT")],
        [KeyboardButton(text=BTN_BACK)],
    ],
    resize_keyboard=True,
)

# ---------------------------------------------------------------------------
# Вспомогалка для одношаговых сценариев (память)
# ---------------------------------------------------------------------------


class Pending(str):
    NONE = "none"
    MEMORY_ADD = "memory_add"


PENDING_ACTIONS: Dict[int, str] = {}

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = Router(name="blackbox")


async def _ensure_user(message: Message) -> Dict:
    return get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _check_limit(message: Message) -> bool:
    user = await _ensure_user(message)
    telegram_id = user["telegram_id"]

    if user["is_admin"] or is_premium(telegram_id):
        return True

    today = _today_str()
    used = get_usage_today(telegram_id, today)

    if used >= FREE_MESSAGES_LIMIT:
        await message.answer(
            "<b>Бесплатный лимит на сегодня исчерпан.</b>\n\n"
            "Чтобы продолжить без ограничений — оформи <b>BlackBox GPT Premium</b>.",
            reply_markup=MAIN_KB,
        )
        return False

    new_count = increment_usage(telegram_id, today)
    logger.info(
        "User %s used free message %s / %s",
        telegram_id,
        new_count,
        FREE_MESSAGES_LIMIT,
    )
    return True


def _format_profile(telegram_id: int) -> str:
    user = get_user(telegram_id)
    if not user:
        return "Профиль не найден."

    memory_text = user["memory"].strip()
    sub = get_active_subscription(telegram_id)
    referrals_count = get_referral_stats(telegram_id)

    parts: list[str] = [
        "🧠 <b>Твой профиль BlackBox GPT</b>",
        "",
        f"ID: <code>{telegram_id}</code>",
    ]
    if user["username"]:
        parts.append(f"Username: @{user['username']}")
    parts.append(f"Текущий режим: <code>{user['mode']}</code>")

    if sub:
        end_dt = datetime.fromtimestamp(sub["end_ts"], tz=timezone.utc)
        parts.append(
            f"Статус: <b>Premium</b> до <code>{end_dt.strftime('%d.%m.%Y')}</code>"
        )
    else:
        parts.append("Статус: <b>Free</b>")

    parts.append(f"Рефералов: <b>{referrals_count}</b>")
    parts.append("")
    parts.append("📂 <b>Личное досье</b>:")

    if memory_text:
        parts.append(memory_text)
    else:
        parts.append("Пока пусто. Добавь пару фактов — и я буду учитывать их в ответах.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# /start + помощь
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user = get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    mode = MODES.get(user["mode"], MODES["universal"])
    crown = "👑 " if user["is_admin"] else ""

    referral_text = ""
    args = (command.args or "").strip() if command else ""
    if user.get("is_new") and args.startswith("ref_"):
        code = args[4:]
        ref_tid = decode_ref_code(code)
        ref_user = get_user(ref_tid) if ref_tid else None
        if ref_user and ref_tid != message.from_user.id:
            if register_referral(ref_tid, message.from_user.id):
                # Бонусы: по 1 дню Premium
                add_subscription(ref_tid, "ref_bonus", 1)
                add_subscription(message.from_user.id, "ref_bonus", 1)
                referral_text = (
                    "\n\n🎁 Ты зашёл по реферальной ссылке.\n"
                    "Тебе начислен <b>1 день BlackBox GPT Premium</b>."
                )
                try:
                    await message.bot.send_message(
                        ref_tid,
                        "👥 По твоей реферальной ссылке пришёл новый пользователь.\n"
                        "Тебе начислен <b>1 день BlackBox GPT Premium</b>.",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to notify referrer %s", ref_tid)

    text = (
        f"Привет, {message.from_user.first_name or 'друг'}!\n\n"
        f"Это <b>{crown}BlackBox GPT</b> — универсальный ассистент, который держит контекст и подстраивается под тебя.\n\n"
        "Просто напиши запрос или нажми <b>«Новый запрос»</b>.\n\n"
        f"Сейчас активен режим: <b>{mode.title}</b>."
        f"{referral_text}"
    )
    await message.answer(text, reply_markup=MAIN_KB)


@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    text = (
        "🧾 <b>Как со мной работать</b>\n\n"
        "• <b>Новый запрос</b> — просто пиши, как человеку.\n"
        "• <b>Режим</b> — выбираешь стиль работы ассистента.\n"
        "• <b>Память / профиль</b> — досье: цели, контекст, особенности общения.\n"
        "• <b>Рефералы</b> — личная ссылка с бонусами Premium.\n"
        "• <b>Подписка</b> — безлимитные сообщения после free-лимита.\n\n"
        "Я не заменяю врачей, юристов и т.д., но помогаю структурировать мысли, "
        "найти варианты действий и увидеть риски."
    )
    await message.answer(text, reply_markup=MAIN_KB)


# ---------------------------------------------------------------------------
# Режимы
# ---------------------------------------------------------------------------


@router.message(F.text == BTN_MODES)
async def show_modes(message: Message) -> None:
    user = await _ensure_user(message)
    active = user["mode"]

    text = (
        "🎛 <b>Режимы BlackBox GPT</b>\n\n"
        "Выбери стиль, в котором мне работать с тобой. Его всегда можно сменить.\n\n"
        + format_modes_list(active)
        + "\n\nНажми нужный режим внизу."
    )
    await message.answer(text, reply_markup=MODES_KB)


@router.message(
    F.text.in_(
        {
            "🧠 Универсальный",
            "🧩 Глубокий разбор",
            "🎯 Фокус / Задачи",
            "🔥 Креатив / Идеи",
            "🧭 Ментор / Мотивация",
        }
    )
)
async def set_mode_handler(message: Message) -> None:
    mapping = {
        "🧠 Универсальный": "universal",
        "🧩 Глубокий разбор": "deep_dive",
        "🎯 Фокус / Задачи": "focus",
        "🔥 Креатив / Идеи": "creative",
        "🧭 Ментор / Мотивация": "mentor",
    }
    mode_code = mapping[message.text]
    set_user_mode(message.from_user.id, mode_code)
    mode = MODES[mode_code]

    await message.answer(
        f"{mode.emoji} Режим переключён на <b>{mode.title}</b>.\n\n"
        "Пиши запрос — я буду отвечать в этом стиле.",
        reply_markup=MAIN_KB,
    )


# ---------------------------------------------------------------------------
# Память / профиль
# ---------------------------------------------------------------------------


@router.message(F.text == BTN_MEMORY)
async def memory_menu(message: Message) -> None:
    await _ensure_user(message)
    profile = _format_profile(message.from_user.id)
    text = (
        profile
        + "\n\n"
        "Что можно сделать:\n"
        "• <b>Добавить факт</b> — записать важную деталь о себе, целях, задачах.\n"
        "• <b>Моё досье</b> — посмотреть, что уже сохранено.\n"
        "• <b>Очистить память</b> — удалить все сохранённые факты."
    )
    await message.answer(text, reply_markup=MEMORY_KB)


@router.message(F.text == BTN_MEMORY_SHOW)
async def memory_show(message: Message) -> None:
    profile = _format_profile(message.from_user.id)
    await message.answer(profile, reply_markup=MEMORY_KB)


@router.message(F.text == BTN_MEMORY_ADD)
async def memory_add_start(message: Message) -> None:
    PENDING_ACTIONS[message.from_user.id] = Pending.MEMORY_ADD
    await message.answer(
        "Напиши одним сообщением то, что мне важно о тебе запомнить.\n\n"
        "Примеры:\n"
        "• чем ты занимаешься;\n"
        "• твои цели на ближайший год;\n"
        "• как тебе комфортнее, чтобы я с тобой общался.",
        reply_markup=MEMORY_KB,
    )


@router.message(F.text == BTN_MEMORY_CLEAR)
async def memory_clear(message: Message) -> None:
    clear_user_memory(message.from_user.id)
    await message.answer(
        "🧹 Память очищена. Я больше не использую старое досье в ответах.",
        reply_markup=MEMORY_KB,
    )


@router.message(F.text == BTN_BACK)
async def back_to_main(message: Message) -> None:
    PENDING_ACTIONS.pop(message.from_user.id, None)
    await message.answer("Возвращаемся в основное меню.", reply_markup=MAIN_KB)


# ---------------------------------------------------------------------------
# Рефералы
# ---------------------------------------------------------------------------


@router.message(F.text == BTN_REFERRAL)
async def referral_menu(message: Message) -> None:
    await _ensure_user(message)
    me = await message.bot.get_me()
    code = encode_ref_code(message.from_user.id)
    link = f"https://t.me/{me.username}?start=ref_{code}"

    count = get_referral_stats(message.from_user.id)
    days_bonus = count  # по одному дню за реферала

    text = (
        "👥 <b>Реферальная программа BlackBox GPT</b>\n\n"
        "Как работает:\n"
        "• Ты отправляешь человеку свою ссылку.\n"
        "• Он заходит в бота через неё.\n"
        "• Ему — <b>1 день Premium</b>, тебе — <b>1 день Premium</b>.\n\n"
        "Твоя личная ссылка:\n"
        f"<code>{link}</code>\n\n"
        f"Уже пришло по ссылке: <b>{count}</b> чел.\n"
        f"Всего бонусных дней Premium: <b>{days_bonus}</b>."
    )
    await message.answer(text, reply_markup=MAIN_KB)


# ---------------------------------------------------------------------------
# Подписка
# ---------------------------------------------------------------------------

PLANS: Dict[str, tuple[str, float, int]] = {
    "1 месяц — 5 USDT": ("sub_1m", 5.0, 30),
    "3 месяца — 12 USDT": ("sub_3m", 12.0, 90),
    "12 месяцев — 60 USDT": ("sub_12m", 60.0, 365),
}


async def _refresh_subscription_status(telegram_id: int) -> Optional[str]:
    """
    Проверяет последний неоплаченный счёт пользователя.
    Если он оплачен — активирует подписку и возвращает текст-уведомление.
    """
    invoice = get_last_pending_invoice(telegram_id)
    if not invoice:
        return None

    try:
        info = await get_crypto_invoice(invoice["invoice_id"])
    except Exception:  # noqa: BLE001
        logger.exception("Failed to refresh invoice status")
        return None

    status = info.get("status")
    if status != "paid":
        return None

    plan_code = invoice["plan_code"]
    duration_days = None
    for _label, (code, _amount, days) in PLANS.items():
        if code == plan_code:
            duration_days = days
            break
    if duration_days is None:
        duration_days = 30

    mark_invoice_paid(invoice["invoice_id"])
    add_subscription(telegram_id, plan_code, duration_days)

    return "✅ Оплата найдена! Подписка BlackBox GPT Premium активирована."


@router.message(F.text == BTN_SUBSCRIPTION)
async def show_subscription(message: Message) -> None:
    await _ensure_user(message)

    # Автоматически пытаемся подтянуть статус последнего счета
    activated_text = await _refresh_subscription_status(message.from_user.id)
    sub = get_active_subscription(message.from_user.id)

    header = "⚡️ <b>Подписка BlackBox GPT Premium</b>\n\n"
    if activated_text and sub:
        header += activated_text + "\n\n"

    if sub:
        end_dt = datetime.fromtimestamp(sub["end_ts"], tz=timezone.utc)
        header += (
            f"Сейчас у тебя уже активна подписка до "
            f"<code>{end_dt.strftime('%d.%m.%Y')}</code>.\n\n"
        )
    else:
        header += (
            f"Бесплатный лимит — <b>{FREE_MESSAGES_LIMIT} сообщений в день</b>.\n"
            "После — безлимитный доступ по подписке.\n\n"
        )

    text = header + (
        "Тарифы:\n"
        "• 1 месяц — 5 USDT\n"
        "• 3 месяца — 12 USDT\n"
        "• 12 месяцев — 60 USDT\n\n"
        "Выбери нужный план ниже:"
    )
    await message.answer(text, reply_markup=SUBSCRIPTION_KB)


@router.message(F.text.in_(set(PLANS.keys())))
async def create_subscription_invoice(message: Message) -> None:
    plan_code, amount, _days = PLANS[message.text]
    desc = f"Подписка BlackBox GPT Premium — {message.text}"

    try:
        invoice = await create_crypto_invoice(
            amount_usdt=amount,
            description=desc,
            payload=json.dumps(
                {"telegram_id": message.from_user.id, "plan_code": plan_code}
            ),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to create invoice")
        await message.answer(
            "😔 Не удалось создать счёт из-за непредвиденной ошибки. Попробуй ещё раз позже.",
            reply_markup=MAIN_KB,
        )
        return

    invoice_id = str(invoice["invoice_id"])
    bot_url = invoice["bot_invoice_url"]
    create_invoice_record(message.from_user.id, invoice_id, plan_code)

    text = (
        "Создаю счёт на оплату, секунду…\n\n"
        "Готово! Чтобы оплатить, просто перейди по ссылке 👇\n"
        f"{bot_url}\n\n"
        "После оплаты вернись в бота и открой меню «Подписка» — статус подтянется автоматически."
    )
    await message.answer(text, reply_markup=MAIN_KB)


@router.message(Command("check_payment"))
async def cmd_check_payment(message: Message) -> None:
    """Служебная команда: ручная проверка последнего счёта."""
    activated_text = await _refresh_subscription_status(message.from_user.id)
    if activated_text:
        await message.answer(
            activated_text + "\n\nТеперь лимитов по сообщениям нет. Погнали работать.",
            reply_markup=MAIN_KB,
        )
    else:
        await message.answer(
            "Пока нет новых оплаченных счетов. "
            "Если ты уже оплатил — подожди пару минут и попробуй ещё раз.",
            reply_markup=MAIN_KB,
        )


# ---------------------------------------------------------------------------
# Чат
# ---------------------------------------------------------------------------


@router.message(F.text == BTN_NEW)
async def new_request_hint(message: Message) -> None:
    await message.answer(
        "Пиши любой вопрос или задачу — я подключусь.\n\n"
        "Можно про работу, здоровье, деньги, отношения, идеи, тексты, код и т.д.",
        reply_markup=MAIN_KB,
    )


@router.message(F.chat.type == "private", F.text)
async def handle_chat(message: Message) -> None:
    # 1) Обработка режимов ввода памяти
    pending = PENDING_ACTIONS.get(message.from_user.id)
    if pending == Pending.MEMORY_ADD and message.text not in {
        BTN_MEMORY_ADD,
        BTN_MEMORY_SHOW,
        BTN_MEMORY_CLEAR,
        BTN_BACK,
    }:
        PENDING_ACTIONS.pop(message.from_user.id, None)
        updated = append_user_memory(message.from_user.id, message.text)
        await message.answer(
            "Записал. Теперь буду учитывать это в ответах.\n\n"
            "Текущее досье:\n"
            f"{updated}",
            reply_markup=MEMORY_KB,
        )
        return

    # 2) Кнопки уже обрабатываются отдельными хендлерами — просто выходим
    if message.text in {
        BTN_NEW,
        BTN_MODES,
        BTN_SUBSCRIPTION,
        BTN_MEMORY,
        BTN_REFERRAL,
        BTN_HELP,
        BTN_MEMORY_ADD,
        BTN_MEMORY_SHOW,
        BTN_MEMORY_CLEAR,
        BTN_BACK,
        "1 месяц — 5 USDT",
        "3 месяца — 12 USDT",
        "12 месяцев — 60 USDT",
    }:
        return

    # 3) Лимиты
    if not await _check_limit(message):
        return

    await _ensure_user(message)

    await message.bot.send_chat_action(
        message.chat.id,
        ChatAction.TYPING,
    )

    try:
        answer = await generate_ai_reply(message.from_user.id, message.text)
    except Exception:  # noqa: BLE001
        logger.exception("LLM error")
        await message.answer(
            "😔 Что-то пошло не так при обращении к модели. Попробуй повторить запрос позже.",
            reply_markup=MAIN_KB,
        )
        return

    await message.answer(answer, reply_markup=MAIN_KB)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    init_db()
    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Starting BlackBox GPT bot polling…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
