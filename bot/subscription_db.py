# bot/subscription_db.py

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

DB_PATH = os.getenv("BOT_DB_PATH", "bot.db")

# Лимит бесплатных запросов и токенов
FREE_REQUESTS_LIMIT = int(os.getenv("FREE_REQUESTS_LIMIT", "3"))
FREE_TOKENS_LIMIT = int(os.getenv("FREE_TOKENS_LIMIT", "6000"))  # ~6k токенов


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Создаём таблицы и недостающие колонки (миграция для токенов и платежей)."""
    with get_conn() as conn:
        # users
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                free_requests_used INTEGER NOT NULL DEFAULT 0,
                free_tokens_used INTEGER NOT NULL DEFAULT 0,
                subscription_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        # payments
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT UNIQUE NOT NULL,
                telegram_id INTEGER NOT NULL,
                plan_code TEXT NOT NULL,
                asset TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                paid_at TEXT
            )
            """
        )

        # Мягкая миграция, если старые таблицы уже есть без free_tokens_used
        try:
            conn.execute(
                "ALTER TABLE users ADD COLUMN free_tokens_used INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def get_or_create_user(telegram_id: int) -> sqlite3.Row:
    """Возвращает пользователя, при отсутствии создаёт нового."""
    now = _utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        if row:
            return row
        conn.execute(
            """
            INSERT INTO users (
                telegram_id, free_requests_used, free_tokens_used,
                subscription_until, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, 0, 0, None, now, now),
        )
        cur = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return cur.fetchone()


def get_user(telegram_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return cur.fetchone()


def has_active_subscription(telegram_id: int) -> bool:
    user = get_user(telegram_id)
    if not user:
        return False
    until = _parse_dt(user["subscription_until"])
    if not until:
        return False
    return until > _utcnow()


def get_usage_info(telegram_id: int) -> Dict[str, Any]:
    """
    Возвращает:
    {
        "used": int,
        "remaining": int,
        "limit": int,
        "tokens_used": int,
        "tokens_remaining": int,
        "tokens_limit": int,
        "has_subscription": bool
    }
    """
    user = get_or_create_user(telegram_id)
    used = int(user["free_requests_used"])
    remaining = max(FREE_REQUESTS_LIMIT - used, 0)

    tokens_used = int(user["free_tokens_used"] or 0)
    tokens_remaining = max(FREE_TOKENS_LIMIT - tokens_used, 0)

    active = has_active_subscription(telegram_id)
    return {
        "used": used,
        "remaining": remaining,
        "limit": FREE_REQUESTS_LIMIT,
        "tokens_used": tokens_used,
        "tokens_remaining": tokens_remaining,
        "tokens_limit": FREE_TOKENS_LIMIT,
        "has_subscription": active,
    }


def register_ai_usage(telegram_id: int) -> None:
    """+1 бесплатный запрос (если ещё есть)."""
    now = _utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE users
            SET free_requests_used = free_requests_used + 1,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (now, telegram_id),
        )


def register_free_tokens_usage(telegram_id: int, tokens: int) -> None:
    """Добавляем использованные токены (для бесплатного режима)."""
    if tokens <= 0:
        return
    now = _utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE users
            SET free_tokens_used = free_tokens_used + ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (tokens, now, telegram_id),
        )


def reset_free_counter(telegram_id: int) -> None:
    """Сброс счётчиков (если понадобится)."""
    now = _utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE users
            SET free_requests_used = 0,
                free_tokens_used = 0,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (now, telegram_id),
        )


def create_payment(
    invoice_id: str,
    telegram_id: int,
    plan_code: str,
    asset: str,
    amount: float,
) -> None:
    """Создаём запись о платеже со статусом pending."""
    now = _utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO payments (
                invoice_id, telegram_id, plan_code, asset, amount, status, created_at, paid_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (invoice_id, telegram_id, plan_code, asset, amount, "pending", now, None),
        )


def mark_payment_paid(invoice_id: str) -> Optional[sqlite3.Row]:
    """Помечаем платёж как оплаченный и возвращаем его строку."""
    now = _utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE payments
            SET status = 'paid',
                paid_at = ?
            WHERE invoice_id = ?
            """,
            (now, invoice_id),
        )
        cur = conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?",
            (invoice_id,),
        )
        return cur.fetchone()


def extend_subscription(telegram_id: int, days: int) -> str:
    """
    Продлеваем (или создаём) подписку на days.
    Возвращаем новую дату окончания (ISO-строка).
    """
    now = _utcnow()
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT subscription_until FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        current_until = _parse_dt(row["subscription_until"]) if row else None
        base = current_until if current_until and current_until > now else now
        new_until = base + timedelta(days=days)
        conn.execute(
            """
            UPDATE users
            SET subscription_until = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (new_until.isoformat(), now.isoformat(), telegram_id),
        )
    return new_until.isoformat()


def get_payment(invoice_id: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?",
            (invoice_id,),
        )
        return cur.fetchone()


def list_payments_for_user(telegram_id: int, limit: int = 10) -> List[sqlite3.Row]:
    """Последние N платежей пользователя."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            SELECT *
            FROM payments
            WHERE telegram_id = ?
            ORDER BY datetime(created_at) DESC
            LIMIT ?
            """,
            (telegram_id, limit),
        )
        return cur.fetchall()


def can_consume_free_tokens(telegram_id: int, tokens_needed: int) -> bool:
    """
    Проверяем, хватает ли бесплатных токенов.
    Если есть подписка — всегда True.
    """
    if tokens_needed <= 0:
        return True

    info = get_usage_info(telegram_id)
    if info["has_subscription"]:
        return True

    return info["tokens_remaining"] >= tokens_needed
