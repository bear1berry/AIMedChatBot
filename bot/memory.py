# bot/memory.py
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import List, Dict, Any

from .config import settings

logger = logging.getLogger(__name__)

_DB_CONN: sqlite3.Connection | None = None


def init_db() -> None:
    global _DB_CONN
    if _DB_CONN is not None:
        return

    _DB_CONN = sqlite3.connect(settings.db_path, check_same_thread=False)
    _DB_CONN.row_factory = sqlite3.Row

    cur = _DB_CONN.cursor()

    # Пользователи
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
        """
    )

    # Сообщения
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            mode TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        """
    )

    _DB_CONN.commit()
    logger.info("DB initialized at %s", settings.db_path)


@contextmanager
def _cursor():
    if _DB_CONN is None:
        init_db()
    cur = _DB_CONN.cursor()
    try:
        yield cur
        _DB_CONN.commit()
    except Exception:
        _DB_CONN.rollback()
        raise


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def register_user(tg_id: int, username: str | None) -> int:
    """
    Регистрирует пользователя, возвращает внутренний user_id.
    """
    username = (username or "").lstrip("@") or None

    with _cursor() as cur:
        cur.execute("SELECT id FROM users WHERE tg_id = ?", (tg_id,))
        row = cur.fetchone()
        if row:
            user_id = row["id"]
            cur.execute(
                "UPDATE users SET username = ?, last_seen = ? WHERE id = ?",
                (username, _now(), user_id),
            )
            return user_id

        cur.execute(
            """
            INSERT INTO users (tg_id, username, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            """,
            (tg_id, username, _now(), _now()),
        )
        return cur.lastrowid


def save_message(tg_id: int, role: str, content: str, mode: str) -> None:
    user_id = register_user(tg_id, None)
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (user_id, role, content, mode, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, role, content, mode, _now()),
        )


def get_history(tg_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute("SELECT id FROM users WHERE tg_id = ?", (tg_id,))
        row = cur.fetchone()
        if not row:
            return []

        user_id = row["id"]
        cur.execute(
            """
            SELECT role, content, mode, created_at
            FROM messages
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = cur.fetchall()

    # Вернём в хронологическом порядке (старые -> новые)
    return list(reversed([dict(r) for r in rows]))


def get_stats() -> Dict[str, Any]:
    """
    Простая статистика для /stats.
    """
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM users")
        users = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) AS c FROM messages")
        msgs = cur.fetchone()["c"]

    return {"users": users, "messages": msgs}
