# bot/subscription_db.py

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, List

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "subscriptions.sqlite3"


@dataclass
class UserSubscription:
    telegram_id: int
    username: Optional[str]
    free_requests_used: int
    free_tokens_used: int
    paid_until: Optional[int]
    current_mode: Optional[str] = None

    @property
    def has_active_subscription(self) -> bool:
        if self.paid_until is None:
            return False
        return self.paid_until > int(time.time())


@dataclass
class Payment:
    id: int
    telegram_id: int
    invoice_id: int
    currency: str
    amount: float
    status: str
    created_at: int
    paid_at: Optional[int]


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Создаёт / мигрирует таблицы users и payments.
    """
    conn = _get_conn()
    cur = conn.cursor()

    # --- USERS ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            free_requests_used INTEGER DEFAULT 0,
            free_tokens_used INTEGER DEFAULT 0,
            paid_until INTEGER,
            current_mode TEXT
        )
        """
    )

    required_user_cols = {
        "telegram_id": "INTEGER PRIMARY KEY",
        "username": "TEXT",
        "free_requests_used": "INTEGER DEFAULT 0",
        "free_tokens_used": "INTEGER DEFAULT 0",
        "paid_until": "INTEGER",
        "current_mode": "TEXT",
    }

    cur.execute("PRAGMA table_info(users)")
    existing_user_cols = {row["name"] for row in cur.fetchall()}

    for col_name, col_def in required_user_cols.items():
        if col_name not in existing_user_cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")

    # --- PAYMENTS ---
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            invoice_id INTEGER NOT NULL,
            currency TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            paid_at INTEGER
        )
        """
    )

    required_payment_cols = {
        "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
        "telegram_id": "INTEGER NOT NULL",
        "invoice_id": "INTEGER NOT NULL",
        "currency": "TEXT NOT NULL",
        "amount": "REAL NOT NULL",
        "status": "TEXT NOT NULL",
        "created_at": "INTEGER NOT NULL",
        "paid_at": "INTEGER",
    }

    cur.execute("PRAGMA table_info(payments)")
    existing_payment_cols = {row["name"] for row in cur.fetchall()}

    for col_name, col_def in required_payment_cols.items():
        if col_name not in existing_payment_cols:
            cur.execute(f"ALTER TABLE payments ADD COLUMN {col_name} {col_def}")

    conn.commit()
    conn.close()


def _row_to_user(row: sqlite3.Row) -> UserSubscription:
    return UserSubscription(
        telegram_id=row["telegram_id"],
        username=row["username"],
        free_requests_used=row["free_requests_used"],
        free_tokens_used=row["free_tokens_used"],
        paid_until=row["paid_until"],
        current_mode=row["current_mode"],
    )


def _row_to_payment(row: sqlite3.Row) -> Payment:
    return Payment(
        id=row["id"],
        telegram_id=row["telegram_id"],
        invoice_id=row["invoice_id"],
        currency=row["currency"],
        amount=row["amount"],
        status=row["status"],
        created_at=row["created_at"],
        paid_at=row["paid_at"],
    )


def get_or_create_user(telegram_id: int, username: Optional[str]) -> UserSubscription:
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            """
            INSERT INTO users
                (telegram_id, username, free_requests_used, free_tokens_used, paid_until, current_mode)
            VALUES (?, ?, 0, 0, NULL, NULL)
            """,
            (telegram_id, username),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()

    conn.close()
    return _row_to_user(row)  # type: ignore[arg-type]


def get_user(telegram_id: int) -> Optional[UserSubscription]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_user(row)


def update_usage(telegram_id: int, add_requests: int, add_tokens: int) -> None:
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT free_requests_used, free_tokens_used FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    if row is None:
        conn.close()
        return

    new_requests = (row["free_requests_used"] or 0) + max(0, add_requests)
    new_tokens = (row["free_tokens_used"] or 0) + max(0, add_tokens)

    cur.execute(
        """
        UPDATE users
        SET free_requests_used = ?, free_tokens_used = ?
        WHERE telegram_id = ?
        """,
        (new_requests, new_tokens, telegram_id),
    )

    conn.commit()
    conn.close()


def grant_subscription(telegram_id: int, months: int = 1) -> None:
    """
    Продлевает (или создаёт) подписку на указанный срок в месяцах.
    """
    now_ts = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT paid_until FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    if row and row["paid_until"]:
        base_ts = max(now_ts, row["paid_until"])
    else:
        base_ts = now_ts

    added_seconds = 30 * 24 * 60 * 60 * months
    new_paid_until = base_ts + added_seconds

    cur.execute(
        "UPDATE users SET paid_until = ? WHERE telegram_id = ?",
        (new_paid_until, telegram_id),
    )

    conn.commit()
    conn.close()


def set_user_mode(telegram_id: int, mode_key: Optional[str]) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET current_mode = ? WHERE telegram_id = ?",
        (mode_key, telegram_id),
    )
    conn.commit()
    conn.close()


def create_payment(
    telegram_id: int,
    invoice_id: int,
    currency: str,
    amount: float,
    status: str,
    created_at: int,
) -> int:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO payments
            (telegram_id, invoice_id, currency, amount, status, created_at, paid_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        """,
        (telegram_id, invoice_id, currency, amount, status, created_at),
    )
    conn.commit()
    payment_id = int(cur.lastrowid)
    conn.close()
    return payment_id


def get_pending_payments() -> List[Payment]:
    """
    Все активные (ещё не оплаченные / не истекшие) инвойсы.
    """
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM payments
        WHERE status = 'active'
        ORDER BY created_at ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return [_row_to_payment(r) for r in rows]


def mark_payment_paid(invoice_id: int, paid_at: int) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE payments
        SET status = 'paid', paid_at = ?
        WHERE invoice_id = ?
        """,
        (paid_at, invoice_id),
    )
    conn.commit()
    conn.close()


def get_recent_payments(limit: int = 30) -> List[Payment]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM payments ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return [_row_to_payment(r) for r in rows]


def get_premium_users(limit: Optional[int] = None) -> List[UserSubscription]:
    now_ts = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()

    sql = """
        SELECT
