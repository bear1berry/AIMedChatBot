import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# Подгружаем .env, чтобы взять путь к БД
load_dotenv()

DB_PATH = os.getenv("SUBSCRIPTION_DB_PATH", "subscription.db")


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Инициализация БД + мягкие миграции:
    - создаём таблицы, если их нет
    - добавляем недостающие колонки в users / payments
    """
    conn = _get_connection()
    cur = conn.cursor()

    # Базовая схема users
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            is_admin INTEGER DEFAULT 0,
            free_messages_used INTEGER DEFAULT 0,
            total_messages INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            premium_until INTEGER,
            created_at INTEGER,
            updated_at INTEGER
        )
        """
    )

    # Базовая схема payments
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER UNIQUE NOT NULL,
            telegram_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER,
            paid_at INTEGER,
            payload TEXT
        )
        """
    )

    # Миграции для users
    cur.execute("PRAGMA table_info(users)")
    existing_cols = {row[1] for row in cur.fetchall()}

    needed_user_columns = {
        "username": "TEXT",
        "is_admin": "INTEGER DEFAULT 0",
        "free_messages_used": "INTEGER DEFAULT 0",
        "total_messages": "INTEGER DEFAULT 0",
        "total_tokens": "INTEGER DEFAULT 0",
        "premium_until": "INTEGER",
        "created_at": "INTEGER",
        "updated_at": "INTEGER",
    }

    for col_name, col_def in needed_user_columns.items():
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_def}")

    # Миграции для payments
    cur.execute("PRAGMA table_info(payments)")
    existing_payment_cols = {row[1] for row in cur.fetchall()}

    needed_payment_columns = {
        "amount": "REAL NOT NULL DEFAULT 0",
        "currency": "TEXT NOT NULL DEFAULT 'TON'",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "created_at": "INTEGER",
        "paid_at": "INTEGER",
        "payload": "TEXT",
    }

    for col_name, col_def in needed_payment_columns.items():
        if col_name not in existing_payment_cols:
            cur.execute(f"ALTER TABLE payments ADD COLUMN {col_name} {col_def}")

    conn.commit()
    conn.close()


def _row_to_user(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "telegram_id": row["telegram_id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
        "free_messages_used": row["free_messages_used"],
        "total_messages": row["total_messages"],
        "total_tokens": row["total_tokens"],
        "premium_until": row["premium_until"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_or_create_user(telegram_id: int, username: Optional[str], is_admin: bool = False) -> Dict[str, Any]:
    """
    Возвращает пользователя из БД или создаёт нового.
    Флаг is_admin обновляется, если ты добавил себя в ADMIN_USERNAMES.
    """
    conn = _get_connection()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    now_ts = int(time.time())

    if row:
        # Обновляем username / is_admin при необходимости
        cur.execute(
            """
            UPDATE users
            SET username = COALESCE(?, username),
                is_admin = CASE WHEN ? THEN 1 ELSE is_admin END,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (username, int(is_admin), now_ts, telegram_id),
        )
        conn.commit()

        cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        user = _row_to_user(row)
        conn.close()
        return user

    # Создаём нового
    cur.execute(
        """
        INSERT INTO users (
            telegram_id, username, is_admin,
            free_messages_used, total_messages, total_tokens,
            premium_until, created_at, updated_at
        )
        VALUES (?, ?, ?, 0, 0, 0, NULL, ?, ?)
        """,
        (telegram_id, username, int(is_admin), now_ts, now_ts),
    )
    conn.commit()

    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    user = _row_to_user(row)
    conn.close()
    return user


def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_user(row)


def increment_usage(
    telegram_id: int,
    tokens_used: int = 0,
    count_free_message: bool = True,
) -> None:
    """
    Обновляем статистику использ
