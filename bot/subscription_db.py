from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

DB_PATH = os.getenv("SUBSCRIPTION_DB_PATH", "subscription.db")


@dataclass
class User:
    telegram_id: int
    username: Optional[str]
    is_admin: bool
    free_messages_used: int
    paid_until: Optional[int]
    created_at: int
    updated_at: int


@dataclass
class Payment:
    id: int
    telegram_id: int
    username: Optional[str]
    invoice_id: str
    invoice_url: str
    amount: int
    currency: str
    status: str
    created_at: int
    paid_at: Optional[int]


@contextmanager
def get_conn() -> Iterable[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Создать таблицы, если их ещё нет. Безопасно вызывать при каждом старте."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id       INTEGER PRIMARY KEY,
                username          TEXT,
                is_admin          INTEGER NOT NULL DEFAULT 0,
                free_messages_used INTEGER NOT NULL DEFAULT 0,
                paid_until        INTEGER,
                created_at        INTEGER NOT NULL,
                updated_at        INTEGER NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username    TEXT,
                invoice_id  TEXT NOT NULL UNIQUE,
                invoice_url TEXT NOT NULL,
                amount      INTEGER NOT NULL,
                currency    TEXT NOT NULL,
                status      TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                paid_at     INTEGER
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_invoice_id ON payments(invoice_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_telegram_id ON payments(telegram_id)")


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        free_messages_used=row["free_messages_used"],
        paid_until=row["paid_until"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _admin_usernames() -> Tuple[str, ...]:
    raw = os.getenv("ADMIN_USERNAMES", "bear1berry")
    items = [x.strip().lower() for x in raw.split(",") if x.strip()]
    return tuple(items)


def get_or_create_user(telegram_id: int, username: Optional[str]) -> User:
    now_ts = int(time.time())
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        if row:
            user = _row_to_user(row)
            # обновим username, если изменился
            if username and username != user.username:
                cur.execute(
                    "UPDATE users SET username = ?, updated_at = ? WHERE telegram_id = ?",
                    (username, now_ts, telegram_id),
                )
                user.username = username
                user.updated_at = now_ts
            return user

        is_admin = 1 if username and username.lower() in _admin_usernames() else 0
        cur.execute(
            """
            INSERT INTO users (telegram_id, username, is_admin, free_messages_used, paid_until, created_at, updated_at)
            VALUES (?, ?, ?, 0, NULL, ?, ?)
            """,
            (telegram_id, username, is_admin, now_ts, now_ts),
        )
        return User(
            telegram_id=telegram_id,
            username=username,
            is_admin=bool(is_admin),
            free_messages_used=0,
            paid_until=None,
            created_at=now_ts,
            updated_at=now_ts,
        )


def increment_free_usage(telegram_id: int, username: Optional[str]) -> int:
    """Увеличить счётчик бесплатных сообщений и вернуть новое значение."""
    now_ts = int(time.time())
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT free_messages_used FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        if row is None:
            # создадим пользователя
            get_or_create_user(telegram_id, username)
            cur.execute("SELECT free_messages_used FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()
        free_used = int(row["free_messages_used"]) + 1
        cur.execute(
            "UPDATE users SET free_messages_used = ?, updated_at = ? WHERE telegram_id = ?",
            (free_used, now_ts, telegram_id),
        )
        return free_used


def user_has_active_subscription(user: User) -> bool:
    if not user.paid_until:
        return False
    return user.paid_until > int(time.time())


def set_subscription_month(telegram_id: int, months: int = 1) -> None:
    """Добавить N месяцев подписки пользователю (от текущей даты или от paid_until, если она в будущем)."""
    now_ts = int(time.time())
    add_seconds = int(30 * 24 * 3600 * months)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT paid_until FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        if row and row["paid_until"] and row["paid_until"] > now_ts:
            base = int(row["paid_until"])
        else:
            base = now_ts
        new_paid_until = base + add_seconds
        cur.execute(
            "UPDATE users SET paid_until = ?, updated_at = ? WHERE telegram_id = ?",
            (new_paid_until, now_ts, telegram_id),
        )


def reset_free_counter(telegram_id: int) -> None:
    now_ts = int(time.time())
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET free_messages_used = 0, updated_at = ? WHERE telegram_id = ?",
            (now_ts, telegram_id),
        )


def create_payment(
    telegram_id: int,
    username: Optional[str],
    invoice_id: str,
    invoice_url: str,
    amount: int,
    currency: str,
) -> Payment:
    now_ts = int(time.time())
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO payments (telegram_id, username, invoice_id, invoice_url, amount, currency, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (telegram_id, username, invoice_id, invoice_url, amount, currency, now_ts),
        )
        payment_id = cur.lastrowid
        return Payment(
            id=payment_id,
            telegram_id=telegram_id,
            username=username,
            invoice_id=invoice_id,
            invoice_url=invoice_url,
            amount=amount,
            currency=currency,
            status="pending",
            created_at=now_ts,
            paid_at=None,
        )


def mark_payment_paid(invoice_id: str, paid_ts: Optional[int] = None) -> None:
    paid_ts = paid_ts or int(time.time())
    with get_conn() as conn:
        conn.execute(
            "UPDATE payments SET status = 'paid', paid_at = ? WHERE invoice_id = ?",
            (paid_ts, invoice_id),
        )


def get_payment_by_invoice(invoice_id: str) -> Optional[Payment]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM payments WHERE invoice_id = ?", (invoice_id,))
        row = cur.fetchone()
        if not row:
            return None
        return Payment(
            id=row["id"],
            telegram_id=row["telegram_id"],
            username=row["username"],
            invoice_id=row["invoice_id"],
            invoice_url=row["invoice_url"],
            amount=row["amount"],
            currency=row["currency"],
            status=row["status"],
            created_at=row["created_at"],
            paid_at=row["paid_at"],
        )


def list_active_subscriptions(limit: int = 50) -> List[User]:
    now_ts = int(time.time())
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM users
            WHERE paid_until IS NOT NULL AND paid_until > ?
            ORDER BY paid_until DESC
            LIMIT ?
            """,
            (now_ts, limit),
        )
        rows = cur.fetchall()
        return [_row_to_user(r) for r in rows]


def list_recent_payments(limit: int = 50) -> List[Payment]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM payments ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        res: List[Payment] = []
        for row in rows:
            res.append(
                Payment(
                    id=row["id"],
                    telegram_id=row["telegram_id"],
                    username=row["username"],
                    invoice_id=row["invoice_id"],
                    invoice_url=row["invoice_url"],
                    amount=row["amount"],
                    currency=row["currency"],
                    status=row["status"],
                    created_at=row["created_at"],
                    paid_at=row["paid_at"],
                )
            )
        return res


def get_admin_stats() -> dict:
    now_ts = int(time.time())
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        total_users = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM users WHERE paid_until IS NOT NULL AND paid_until > ?",
            (now_ts,),
        )
        active_subs = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments WHERE status = 'paid'"
        )
        row = cur.fetchone()
        total_payments = row[0]
        total_revenue = row[1]

        return {
            "total_users": total_users,
            "active_subscriptions": active_subs,
            "total_payments": total_payments,
            "total_revenue": total_revenue,
        }
