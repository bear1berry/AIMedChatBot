from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

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

    # Saved clinical cases
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cases (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            title      TEXT NOT NULL,
            summary    TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )

    # Saved notes
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            title      TEXT NOT NULL,
            body       TEXT NOT NULL,
            created_at INTEGER NOT NULL
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
    history: List[dict],
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


def create_case(user_id: int, title: str, summary: str) -> int:
    """
    Сохраняет клинический случай и возвращает его ID.
    """
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "INSERT INTO cases (user_id, title, summary, created_at) VALUES (?, ?, ?, ?)",
        (user_id, title, summary, now),
    )
    conn.commit()
    case_id = cur.lastrowid
    cur.close()
    return case_id


def list_cases(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Возвращает список последних сохранённых случаев пользователя.
    """
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, summary, created_at
        FROM cases
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


def create_note(user_id: int, title: str, body: str) -> int:
    """
    Сохраняет заметку и возвращает её ID.
    """
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "INSERT INTO notes (user_id, title, body, created_at) VALUES (?, ?, ?, ?)",
        (user_id, title, body, now),
    )
    conn.commit()
    note_id = cur.lastrowid
    cur.close()
    return note_id


def list_notes(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Возвращает последние заметки пользователя.
    """
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, body, created_at
        FROM notes
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]


def search_notes(user_id: int, query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Поиск по заметкам пользователя по подстроке в заголовке или тексте.
    """
    conn = _get_conn()
    cur = conn.cursor()
    pattern = f"%{query}%"
    cur.execute(
        """
        SELECT id, title, body, created_at
        FROM notes
        WHERE user_id = ?
          AND (title LIKE ? OR body LIKE ?)
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, pattern, pattern, limit),
    )
    rows = cur.fetchall()
    cur.close()
    return [dict(r) for r in rows]
