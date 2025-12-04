# bot/subscription_db.py

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

DB_PATH = os.getenv("BOT_DB_PATH", "bot.db")
FREE_REQUESTS_LIMIT = int(os.getenv("FREE_REQUESTS_LIMIT", "3"))


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
    """Создаём таблицы для пользователей и платежей (если их ещё нет)."""
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                free_requests_used INTEGER NOT NULL DEFAULT 0,
                subscription_until TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
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
                telegram_id, free_requests_used, subscription_until, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_id, 0, None, now, now),
        )
        cur = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return cur.fetchone()


def get_user(telegram_id: int) -> Optional[sqlite3.Row]:
    """Возвращает пользователя или None."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return cur.fetchone()


def has_active_subscription(telegram_id: int) -> bool:
    """Проверка, активна ли подписка у пользователя."""
    user = get_user(telegram_id)
    if not user:
        return False
    until = _parse_dt(user["subscription_until"])
    if not until:
        return False
    return until > _utcnow()


def get_usage_info(telegram_id: int) -> Dict[str, Any]:
    """
    Возвращает словарь:
    {
        "used": int,
        "remaining": int,
        "limit": int,
        "has_subscription": bool
    }
    """
    user = get_or_create_user(telegram_id)
    used = int(user["free_requests_used"])
    remaining = max(FREE_REQUESTS_LIMIT - used, 0)
    active = has_active_subscription(telegram_id)
    return {
        "used": used,
        "remaining": remaining,
        "limit": FREE_REQUESTS_LIMIT,
        "has_subscription": active,
    }


def register_ai_usage(telegram_id: int) -> None:
    """Увеличиваем счётчик использованных бесплатных запросов."""
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


def reset_free_counter(telegram_id: int) -> None:
    """Обнуление счётчика бесплатных запросов (на будущее, если пригодится)."""
    now = _utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE users
            SET free_requests_used = 0,
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
    """
    Помечаем платёж как оплаченный.
    Возвращаем строку с платежом (после обновления) или None, если не нашли.
    """
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
    Продлеваем (или создаём) подписку на заданное количество дней.
    Возвращаем новую дату окончания в ISO-формате.
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
    """Возвращаем платёж по invoice_id."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM payments WHERE invoice_id = ?",
            (invoice_id,),
        )
        return cur.fetchone()
