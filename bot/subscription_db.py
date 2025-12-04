import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple, Any

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("SUBSCRIPTION_DB_PATH", "subscription.db")


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
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                free_messages_used INTEGER NOT NULL DEFAULT 0,
                total_messages INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                premium_until INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT UNIQUE NOT NULL,
                telegram_id INTEGER NOT NULL,
                amount REAL,
                currency TEXT,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                paid_at INTEGER
            )
            """
        )


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def get_or_create_user(telegram_id: int, username: str, is_admin: bool) -> Dict[str, Any]:
    now_ts = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        if row:
            # при каждом заходе обновляем username и is_admin
            cur.execute(
                """
                UPDATE users
                SET username = ?, is_admin = ?, updated_at = ?
                WHERE telegram_id = ?
                """,
                (username, int(is_admin), now_ts, telegram_id),
            )
            conn.commit()
            cur.execute(
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            return _row_to_dict(cur.fetchone())

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
        cur.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return _row_to_dict(cur.fetchone())


def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(row) if row else None


def increment_free_messages_used(telegram_id: int, delta: int = 1) -> None:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET free_messages_used = free_messages_used + ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (delta, int(time.time()), telegram_id),
        )


def increment_usage(
    telegram_id: int,
    messages_delta: int = 0,
    tokens_delta: int = 0,
) -> None:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET
                total_messages = total_messages + ?,
                total_tokens = total_tokens + ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (messages_delta, tokens_delta, int(time.time()), telegram_id),
        )
        # считаем эти сообщения и как бесплатные (для лимита),
        # лимит всё равно игнорируется для активного премиума/админа
        if messages_delta > 0:
            cur.execute(
                """
                UPDATE users
                SET free_messages_used = free_messages_used + ?
                WHERE telegram_id = ?
                """,
                (messages_delta, telegram_id),
            )


def upgrade_user_to_premium(telegram_id: int, days: int = 30, paid_at: Optional[int] = None) -> None:
    if paid_at is None:
        paid_at = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT premium_u_
