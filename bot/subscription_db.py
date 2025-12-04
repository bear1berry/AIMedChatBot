# bot/subscription_db.py

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DB_PATH = Path(os.getenv("BOT_DB_PATH", "bot.db"))


@dataclass
class UserSubscription:
    telegram_id: int
    username: Optional[str]
    free_requests_used: int
    free_tokens_used: int
    paid_until: Optional[int]  # unix timestamp или None

    @property
    def has_active_subscription(self) -> bool:
        if not self.paid_until:
            return False
        return self.paid_until > int(time.time())


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _get_columns(conn: sqlite3.Connection) -> set[str]:
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(users)")
    rows = cur.fetchall()
    return {row["name"] for row in rows}


def init_db() -> None:
    """
    Инициализация БД + мягкая миграция старой схемы.
    Раньше таблица users могла быть без полей username / free_tokens_used / paid_until.
    Здесь мы аккуратно добавляем недостающие колонки.
    """
    conn = _get_conn()
    cur = conn.cursor()

    # Есть ли таблица users вообще?
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
    )
    row = cur.fetchone()

    if not row:
        # Создаём таблицу с новой схемой
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    free_requests_used INTEGER NOT NULL DEFAULT 0,
                    free_tokens_used INTEGER NOT NULL DEFAULT 0,
                    paid_until INTEGER
                )
                """
            )
        conn.close()
        return

    # Таблица есть — проверим колонки и добавим недостающие
    existing_cols = _get_columns(conn)
    required_cols: dict[str, str] = {
        "username": "TEXT",
        "free_requests_used": "INTEGER NOT NULL DEFAULT 0",
        "free_tokens_used": "INTEGER NOT NULL DEFAULT 0",
        "paid_until": "INTEGER",
    }

    for col, col_type in required_cols.items():
        if col not in existing_cols:
            with conn:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")

    conn.close()


def _row_to_user(row: sqlite3.Row) -> UserSubscription:
    # На всякий случай берём через keys(), чтобы не падать,
    # если вдруг схема ещё не обновилась.
    keys = set(row.keys())
    username = row["username"] if "username" in keys else None
    free_requests_used = (
        int(row["free_requests_used"]) if "free_requests_used" in keys else 0
    )
    free_tokens_used = (
        int(row["free_tokens_used"]) if "free_tokens_used" in keys else 0
    )
    paid_until = row["paid_until"] if "paid_until" in keys else None

    return UserSubscription(
        telegram_id=row["telegram_id"],
        username=username,
        free_requests_used=free_requests_used,
        free_tokens_used=free_tokens_used,
        paid_until=paid_until,
    )


def get_or_create_user(telegram_id: int, username: Optional[str] = None) -> UserSubscription:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    if row:
        user = _row_to_user(row)
        # обновим username при необходимости
        if username and username != user.username:
            with conn:
                conn.execute(
                    "UPDATE users SET username = ? WHERE telegram_id = ?",
                    (username, telegram_id),
                )
            user.username = username
    else:
        with conn:
            conn.execute(
                "INSERT INTO users (telegram_id, username, free_requests_used, free_tokens_used, paid_until) "
                "VALUES (?, ?, 0, 0, NULL)",
                (telegram_id, username),
            )
        user = UserSubscription(
            telegram_id=telegram_id,
            username=username,
            free_requests_used=0,
            free_tokens_used=0,
            paid_until=None,
        )
    conn.close()
    return user


def get_user(telegram_id: int) -> Optional[UserSubscription]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_user(row)


def add_free_usage(telegram_id: int, add_requests: int, add_tokens: int) -> None:
    conn = _get_conn()
    with conn:
        conn.execute(
            """
            UPDATE users
            SET free_requests_used = COALESCE(free_requests_used, 0) + ?,
                free_tokens_used = COALESCE(free_tokens_used, 0) + ?
            WHERE telegram_id = ?
            """,
            (add_requests, add_tokens, telegram_id),
        )
    conn.close()


def grant_subscription(telegram_id: int, days: int) -> None:
    """Добавляем/продлеваем подписку на N дней."""
    now = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()

    # Убедимся, что колонка paid_until есть (на случай очень старой БД)
    cols = _get_columns(conn)
    if "paid_until" not in cols:
        with conn:
            conn.execute("ALTER TABLE users ADD COLUMN paid_until INTEGER")

    cur.execute("SELECT paid_until FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    if row and row["paid_until"]:
        base = max(now, int(row["paid_until"]))
    else:
        base = now

    new_paid_until = base + days * 86400

    with conn:
        conn.execute(
            "UPDATE users SET paid_until = ? WHERE telegram_id = ?",
            (new_paid_until, telegram_id),
        )
    conn.close()
