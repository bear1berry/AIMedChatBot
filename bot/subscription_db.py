# bot/subscription_db.py

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any

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


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Создаёт таблицу users, если её нет,
    и гарантирует наличие всех нужных колонок.
    """
    conn = _get_conn()
    cur = conn.cursor()

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

    # Проверяем наличие колонок и добавляем недостающие (на случай старой БД)
    required_cols = {
        "telegram_id": "INTEGER PRIMARY KEY",
        "username": "TEXT",
        "free_requests_used": "INTEGER DEFAULT 0",
        "free_tokens_used": "INTEGER DEFAULT 0",
        "paid_until": "INTEGER",
        "current_mode": "TEXT",
    }

    cur.execute("PRAGMA table_info(users)")
    existing_cols = {row["name"] for row in cur.fetchall()}

    for col_name, col_def in required_cols.items():
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")

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


def get_or_create_user(telegram_id: int, username: Optional[str]) -> UserSubscription:
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            """
            INSERT INTO users (telegram_id, username, free_requests_used, free_tokens_used, paid_until, current_mode)
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

    # Простейшая модель: 30 дней * months
    added_seconds = 30 * 24 * 60 * 60 * months
    new_paid_until = base_ts + added_seconds

    cur.execute(
        "UPDATE users SET paid_until = ? WHERE telegram_id = ?",
        (new_paid_until, telegram_id),
    )

    conn.commit()
    conn.close()


def set_user_mode(telegram_id: int, mode_key: Optional[str]) -> None:
    """
    Сохраняет текущий режим работы бота для пользователя.
    mode_key должен быть либо ключом из MODES, либо None.
    """
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET current_mode = ? WHERE telegram_id = ?",
        (mode_key, telegram_id),
    )
    conn.commit()
    conn.close()


def get_stats() -> Dict[str, Any]:
    """
    Простая статистика для /admin.
    """
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) as cnt FROM users")
    total_users = cur.fetchone()["cnt"]

    now_ts = int(time.time())
    cur.execute(
        "SELECT COUNT(*) as cnt FROM users WHERE paid_until IS NOT NULL AND paid_until > ?",
        (now_ts,),
    )
    active_premium_users = cur.fetchone()["cnt"]

    conn.close()
    return {
        "total_users": int(total_users),
        "active_premium_users": int(active_premium_users),
    }
