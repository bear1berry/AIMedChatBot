from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "subscriptions_v2.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Initialize SQLite database with users and payments tables.
    Safe to call multiple times.
    """
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id      INTEGER PRIMARY KEY,
            username         TEXT,
            is_admin         INTEGER NOT NULL DEFAULT 0,
            free_requests    INTEGER NOT NULL DEFAULT 0,
            free_tokens      INTEGER NOT NULL DEFAULT 0,
            paid_until_ts    INTEGER,
            created_at_ts    INTEGER NOT NULL,
            updated_at_ts    INTEGER NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id      INTEGER NOT NULL,
            invoice_id       TEXT NOT NULL UNIQUE,
            asset            TEXT NOT NULL,
            amount           REAL NOT NULL,
            status           TEXT NOT NULL,
            payload          TEXT,
            created_at_ts    INTEGER NOT NULL,
            paid_at_ts       INTEGER,
            FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
        )
        """
    )

    conn.commit()
    conn.close()
    logger.info("Subscription DB initialized at %s", DB_PATH)


# Backwards-compatible alias (на случай старых импортах)
def init_subscription_storage() -> None:
    init_db()


@dataclass
class User:
    telegram_id: int
    username: Optional[str]
    is_admin: bool
    free_requests: int
    free_tokens: int
    paid_until_ts: Optional[int]
    created_at_ts: int
    updated_at_ts: int


@dataclass
class Payment:
    id: int
    telegram_id: int
    invoice_id: str
    asset: str
    amount: float
    status: str
    payload: Optional[str]
    created_at_ts: int
    paid_at_ts: Optional[int]


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        telegram_id=row["telegram_id"],
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        free_requests=row["free_requests"],
        free_tokens=row["free_tokens"],
        paid_until_ts=row["paid_until_ts"],
        created_at_ts=row["created_at_ts"],
        updated_at_ts=row["updated_at_ts"],
    )


def _row_to_payment(row: sqlite3.Row) -> Payment:
    return Payment(
        id=row["id"],
        telegram_id=row["telegram_id"],
        invoice_id=row["invoice_id"],
        asset=row["asset"],
        amount=row["amount"],
        status=row["status"],
        payload=row["payload"],
        created_at_ts=row["created_at_ts"],
        paid_at_ts=row["paid_at_ts"],
    )


def get_or_create_user(telegram_id: int, username: Optional[str]) -> User:
    """
    Fetch user from DB or create a new one.
    """
    init_db()
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    now = int(time.time())
    if row is None:
        cur.execute(
            """
            INSERT INTO users (telegram_id, username, is_admin, free_requests,
                               free_tokens, paid_until_ts, created_at_ts, updated_at_ts)
            VALUES (?, ?, 0, 0, 0, NULL, ?, ?)
            """,
            (telegram_id, username, now, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
    else:
        # update username if it changed
        if username and row["username"] != username:
            cur.execute(
                "UPDATE users SET username = ?, updated_at_ts = ? WHERE telegram_id = ?",
                (username, now, telegram_id),
            )
            conn.commit()
            cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()

    conn.close()
    assert row is not None
    return _row_to_user(row)


def get_user(telegram_id: int) -> Optional[User]:
    init_db()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return _row_to_user(row) if row else None


def set_admin(telegram_id: int, is_admin: bool = True) -> None:
    init_db()
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "UPDATE users SET is_admin = ?, updated_at_ts = ? WHERE telegram_id = ?",
        (1 if is_admin else 0, now, telegram_id),
    )
    conn.commit()
    conn.close()


def increment_free_usage(telegram_id: int, tokens_used: int) -> None:
    """
    Increase counters for free usage.
    """
    init_db()
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        """
        UPDATE users
        SET free_requests = free_requests + 1,
            free_tokens = free_tokens + ?,
            updated_at_ts = ?
        WHERE telegram_id = ?
        """,
        (tokens_used, now, telegram_id),
    )
    conn.commit()
    conn.close()


def set_subscription_month(telegram_id: int, months: int = 1) -> None:
    """
    Extend paid_until_ts by given number of months (30 days each).
    If no subscription yet, start from now.
    """
    init_db()
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "SELECT paid_until_ts FROM users WHERE telegram_id = ?", (telegram_id,)
    )
    row = cur.fetchone()
    base = max(now, row["paid_until_ts"]) if row and row["paid_until_ts"] else now
    added = 30 * 24 * 60 * 60 * months
    new_until = base + added
    cur.execute(
        "UPDATE users SET paid_until_ts = ?, updated_at_ts = ? WHERE telegram_id = ?",
        (new_until, now, telegram_id),
    )
    conn.commit()
    conn.close()


def user_has_active_subscription(user: User) -> bool:
    if user.is_admin:
        return True
    if user.paid_until_ts is None:
        return False
    return user.paid_until_ts > int(time.time())


def create_payment(
    telegram_id: int,
    invoice_id: str,
    asset: str,
    amount: float,
    payload: Optional[str] = None,
    status: str = "active",
) -> Payment:
    init_db()
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        """
        INSERT INTO payments (
            telegram_id, invoice_id, asset, amount, status,
            payload, created_at_ts, paid_at_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (telegram_id, invoice_id, asset, amount, status, payload, now),
    )
    conn.commit()
    cur.execute("SELECT * FROM payments WHERE invoice_id = ?", (invoice_id,))
    row = cur.fetchone()
    conn.close()
    assert row is not None
    return _row_to_payment(row)


def get_last_payment(telegram_id: int) -> Optional[Payment]:
    init_db()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM payments
        WHERE telegram_id = ?
        ORDER BY created_at_ts DESC
        LIMIT 1
        """,
        (telegram_id,),
    )
    row = cur.fetchone()
    conn.close()
    return _row_to_payment(row) if row else None


def mark_payment_paid(invoice_id: str, paid_at_ts: Optional[int] = None) -> None:
    init_db()
    conn = _get_conn()
    cur = conn.cursor()
    if paid_at_ts is None:
        paid_at_ts = int(time.time())
    cur.execute(
        """
        UPDATE payments
        SET status = 'paid',
            paid_at_ts = ?
        WHERE invoice_id = ?
        """,
        (paid_at_ts, invoice_id),
    )
    conn.commit()
    conn.close()


def get_admin_stats() -> Dict[str, Any]:
    init_db()
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]

    now = int(time.time())
    cur.execute(
        "SELECT COUNT(*) FROM users WHERE paid_until_ts IS NOT NULL AND paid_until_ts > ?",
        (now,),
    )
    active_subs = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM users WHERE free_requests > 0 OR free_tokens > 0"
    )
    used_free = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(*) FROM users
        WHERE paid_until_ts IS NULL AND free_requests = 0 AND free_tokens = 0
        """
    )
    never_used = cur.fetchone()[0]

    conn.close()
    return {
        "total_users": total_users,
        "active_subscriptions": active_subs,
        "used_free": used_free,
        "never_used": never_used,
    }


def list_active_subscriptions(limit: int = 50) -> List[User]:
    init_db()
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        """
        SELECT * FROM users
        WHERE paid_until_ts IS NOT NULL AND paid_until_ts > ?
        ORDER BY paid_until_ts DESC
        LIMIT ?
        """,
        (now, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [_row_to_user(r) for r in rows]


def list_recent_payments(limit: int = 50) -> List[Payment]:
    init_db()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM payments
        ORDER BY created_at_ts DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return [_row_to_payment(r) for r in rows]
