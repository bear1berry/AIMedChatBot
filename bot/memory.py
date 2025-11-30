from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import settings

logger = logging.getLogger(__name__)

_CONN: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        _CONN = sqlite3.connect(settings.db_path, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _init_db(_CONN)
        logger.info("DB initialized at %s", settings.db_path)
    return _CONN


def _init_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            role        TEXT,
            detail_level TEXT,
            jurisdiction TEXT,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            title       TEXT NOT NULL,
            is_active   INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id     INTEGER NOT NULL,
            role        TEXT NOT NULL, -- 'user' / 'assistant'
            content     TEXT NOT NULL,
            mode        TEXT,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()


@contextmanager
def _cursor():
    conn = _get_conn()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def register_user(user_id: int, username: str | None) -> None:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
            """,
            (user_id, (username or "")[:64]),
        )


def set_profile(user_id: int, username: str | None, role: str, detail_level: str, jurisdiction: str) -> None:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (user_id, username, role, detail_level, jurisdiction)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                role=excluded.role,
                detail_level=excluded.detail_level,
                jurisdiction=excluded.jurisdiction
            """,
            (user_id, (username or "")[:64], role, detail_level, jurisdiction),
        )


def get_profile(user_id: int) -> Dict[str, str]:
    with _cursor() as cur:
        cur.execute("SELECT role, detail_level, jurisdiction FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
    if not row:
        # значения по умолчанию
        return {"role": "пациент", "detail_level": "стандарт", "jurisdiction": "GLOBAL"}
    return {
        "role": row["role"] or "пациент",
        "detail_level": row["detail_level"] or "стандарт",
        "jurisdiction": row["jurisdiction"] or "GLOBAL",
    }


def create_conversation(user_id: int, title: str) -> int:
    title = title.strip() or "Новый диалог"
    with _cursor() as cur:
        # деактивируем старые
        cur.execute("UPDATE conversations SET is_active = 0 WHERE user_id = ?", (user_id,))
        cur.execute(
            "INSERT INTO conversations (user_id, title, is_active) VALUES (?, ?, 1)",
            (user_id, title[:120]),
        )
        conv_id = cur.lastrowid
    return int(conv_id)


def get_active_conversation(user_id: int) -> int:
    with _cursor() as cur:
        cur.execute(
            "SELECT id FROM conversations WHERE user_id = ? AND is_active = 1 ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
    if row:
        return int(row["id"])
    # создаём по умолчанию
    return create_conversation(user_id, "Диалог")


def list_conversations(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    with _cursor() as cur:
        cur.execute(
            """
            SELECT id, title, created_at, is_active
            FROM conversations
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def save_message(user_id: int, role: str, content: str, mode: str = "default") -> None:
    conv_id = get_active_conversation(user_id)
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO messages (conv_id, role, content, mode) VALUES (?, ?, ?, ?)",
            (conv_id, role, content, mode),
        )


def get_history(user_id: int, limit: int = 12) -> List[Dict[str, str]]:
    conv_id = get_active_conversation(user_id)
    with _cursor() as cur:
        cur.execute(
            """
            SELECT role, content
            FROM messages
            WHERE conv_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (conv_id, limit),
        )
        rows = cur.fetchall()
    # возвращаем в хронологическом порядке
    return [dict(r) for r in reversed(rows)]


def get_stats() -> Dict[str, int]:
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM users")
        users = cur.fetchone()["c"] or 0
        cur.execute("SELECT COUNT(*) AS c FROM messages")
        msgs = cur.fetchone()["c"] or 0
    return {"users": int(users), "messages": int(msgs)}
