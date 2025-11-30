import sqlite3
from typing import List, Dict
from datetime import datetime
import logging

from .config import settings

logger = logging.getLogger(__name__)

DB_PATH = settings.db_path

# Для команды /users (в памяти – кто уже писал с момента запуска)
active_users: dict[int, str] = {}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_seen  TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen   TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                role       TEXT NOT NULL,
                text       TEXT NOT NULL,
                mode       TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
        logger.info("DB initialized at %s", DB_PATH)
    finally:
        conn.close()


def register_user(user_id: int, username: str | None) -> None:
    username = username or ""
    active_users[user_id] = username

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO users (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                last_seen=CURRENT_TIMESTAMP
            """,
            (user_id, username),
        )
        conn.commit()
    finally:
        conn.close()


def log_message(user_id: int, role: str, text: str, mode: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO messages (user_id, role, text, mode) VALUES (?, ?, ?, ?)",
            (user_id, role, text, mode),
        )
        conn.commit()
    finally:
        conn.close()


def get_history(user_id: int, limit: int = 10) -> List[Dict]:
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT role, text, mode, created_at
            FROM messages
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    # возвращаем в хронологическом порядке
    return [
        {"role": row["role"], "text": row["text"], "mode": row["mode"], "created_at": row["created_at"]}
        for row in reversed(rows)
    ]


def get_users(limit: int = 50) -> List[sqlite3.Row]:
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT user_id, username, last_seen
            FROM users
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return rows
