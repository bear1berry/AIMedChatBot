from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Optional

from .config import settings

_CONN: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    """
    Singleton-коннект к SQLite + инициализация схемы.
    Используется и лимитами, и клиентом ИИ.
    """
    global _CONN
    if _CONN is None:
        _CONN = sqlite3.connect(settings.db_path, check_same_thread=False)
        _CONN.row_factory = sqlite3.Row
        _init_db(_CONN)
    return _CONN


def _init_db(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    # Conversation state
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            user_id       INTEGER PRIMARY KEY,
            mode_key      TEXT NOT NULL,
            history_json  TEXT NOT NULL,
            last_question TEXT,
            last_answer   TEXT,
            verbosity     TEXT NOT NULL DEFAULT 'normal',
            tone          TEXT NOT NULL DEFAULT 'neutral',
            format_pref   TEXT NOT NULL DEFAULT 'auto',
            updated_at    INTEGER NOT NULL
        )
        """
    )

    # Rate limits
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limits (
            user_id      INTEGER NOT NULL,
            scope        TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            count        INTEGER NOT NULL,
            PRIMARY KEY (user_id, scope)
        )
        """
    )

    conn.commit()
    cur.close()


def load_conversation_row(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Возвращает dict с полями из conversations или None.
    """
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            mode_key,
            history_json,
            last_question,
            last_answer,
            verbosity,
            tone,
            format_pref
        FROM conversations
        WHERE user_id = ?
        """,
        (user_id,),
    )
    row = cur.fetchone()
    cur.close()
    if row is None:
        return None
    return dict(row)


def save_conversation_row(
    user_id: int,
    mode_key: str,
    history: list[dict],
    last_question: Optional[str],
    last_answer: Optional[str],
    verbosity: str,
    tone: str,
    format_pref: str,
) -> None:
    """
    Upsert состояния диалога.
    """
    conn = _get_conn()
    cur = conn.cursor()
    history_json = json.dumps(history, ensure_ascii=False)
    now = int(time.time())
    cur.execute(
        """
        INSERT INTO conversations (
            user_id,
            mode_key,
            history_json,
            last_question,
            last_answer,
            verbosity,
            tone,
            format_pref,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            mode_key      = excluded.mode_key,
            history_json  = excluded.history_json,
            last_question = excluded.last_question,
            last_answer   = excluded.last_answer,
            verbosity     = excluded.verbosity,
            tone          = excluded.tone,
            format_pref   = excluded.format_pref,
            updated_at    = excluded.updated_at
        """,
        (
            user_id,
            mode_key,
            history_json,
            last_question,
            last_answer,
            verbosity,
            tone,
            format_pref,
            now,
        ),
    )
    conn.commit()
    cur.close()


def clear_history(user_id: int) -> None:
    """
    Чистит историю и последний Q/A, не трогая режим и настройки.
    """
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        """
        UPDATE conversations
        SET history_json = '[]',
            last_question = NULL,
            last_answer   = NULL,
            updated_at    = ?
        WHERE user_id = ?
        """,
        (now, user_id),
    )
    conn.commit()
    cur.close()
