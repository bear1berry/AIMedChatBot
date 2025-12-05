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


def _row_to_user(row):
    """
    Преобразует строку из таблицы users в питоновский словарь.
    Работает и со старой схемой (без колонки id), и с новой.
    """
    if row is None:
        return None

    # Какие ключи реально есть в строке
    keys = set(row.keys())

    # Определяем основной идентификатор пользователя
    tg_id = None
    if "telegram_id" in keys:
        tg_id = row["telegram_id"]
    elif "user_id" in keys:
        tg_id = row["user_id"]
    elif "id" in keys:
        tg_id = row["id"]

    # Базовые поля с безопасными значениями по умолчанию
    username = row["username"] if "username" in keys else None
    first_name = row["first_name"] if "first_name" in keys else None
    last_name = row["last_name"] if "last_name" in keys else None

    is_admin = row["is_admin"] if "is_admin" in keys else 0
    is_premium = row["is_premium"] if "is_premium" in keys else 0
    premium_until = row["premium_until"] if "premium_until" in keys else 0
    total_messages = row["total_messages"] if "total_messages" in keys else 0

    # Возвращаем и "id" (для обратной совместимости), и telegram_id
    return {
        "id": tg_id,                    # чтобы старый код, который ждал user["id"], не упал
        "telegram_id": tg_id,
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "is_admin": bool(is_admin),
        "is_premium": bool(is_premium),
        "premium_until": int(premium_until or 0),
        "total_messages": int(total_messages or 0),
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
    Обновляем статистику использования:
    - total_messages +1
    - total_tokens + tokens_used
    - free_messages_used +1 (если пользователь не в премиуме и count_free_message=True)
    """
    conn = _get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT free_messages_used, premium_until FROM users WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return

    free_used = row["free_messages_used"] or 0
    premium_until = row["premium_until"]
    now_ts = int(time.time())
    is_premium = bool(premium_until and premium_until > now_ts)

    if (not is_premium) and count_free_message:
        free_used += 1

    cur.execute(
        """
        UPDATE users
        SET free_messages_used = ?,
            total_messages = total_messages + 1,
            total_tokens = total_tokens + ?,
            updated_at = ?
        WHERE telegram_id = ?
        """,
        (free_used, tokens_used, now_ts, telegram_id),
    )
    conn.commit()
    conn.close()


def upgrade_user_to_premium(telegram_id: int, days: int) -> None:
    """
    Выдаём / продлеваем премиум пользователю.
    """
    seconds = days * 24 * 60 * 60
    now_ts = int(time.time())

    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT premium_until FROM users WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = cur.fetchone()

    if row and row["premium_until"]:
        base = max(row["premium_until"], now_ts)
    else:
        base = now_ts

    new_until = base + seconds

    cur.execute(
        """
        UPDATE users
        SET premium_until = ?, free_messages_used = 0, updated_at = ?
        WHERE telegram_id = ?
        """,
        (new_until, now_ts, telegram_id),
    )
    conn.commit()
    conn.close()


def save_payment(
    invoice_id: int,
    telegram_id: int,
    amount: float,
    currency: str,
    status: str,
    created_at: Optional[int],
    payload: Optional[str],
) -> None:
    conn = _get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT OR REPLACE INTO payments (
            invoice_id, telegram_id, amount, currency,
            status, created_at, paid_at, payload
        )
        VALUES (
            ?, ?, ?, ?,
            ?, ?, COALESCE(
                (SELECT paid_at FROM payments WHERE invoice_id = ?),
                NULL
            ),
            ?
        )
        """,
        (
            invoice_id,
            telegram_id,
            amount,
            currency,
            status,
            created_at,
            invoice_id,
            payload,
        ),
    )
    conn.commit()
    conn.close()


def update_payment_status(
    invoice_id: int,
    status: str,
    paid_at: Optional[int],
) -> None:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE payments
        SET status = ?, paid_at = COALESCE(?, paid_at)
        WHERE invoice_id = ?
        """,
        (status, paid_at, invoice_id),
    )
    conn.commit()
    conn.close()


def get_pending_payments() -> List[Dict[str, Any]]:
    """
    Список счетов, которые ещё не оплачены / не отменены.
    """
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM payments
        WHERE status IN ('active', 'pending', 'wait')
        ORDER BY created_at DESC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def mark_payment_and_upgrade(
    invoice_id: int,
    paid_at: int,
    premium_days: int,
) -> None:
    """
    Помечаем платёж как оплаченный и выдаём премиум пользователю.
    """
    conn = _get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT telegram_id FROM payments WHERE invoice_id = ?",
        (invoice_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return

    telegram_id = row["telegram_id"]

    cur.execute(
        """
        UPDATE payments
        SET status = 'paid', paid_at = ?
        WHERE invoice_id = ?
        """,
        (paid_at, invoice_id),
    )
    conn.commit()
    conn.close()

    # Выдаём премиум
    upgrade_user_to_premium(telegram_id, premium_days)


def get_total_users_count() -> int:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    (count,) = cur.fetchone()
    conn.close()
    return int(count or 0)


def get_active_premium_count() -> int:
    now_ts = int(time.time())
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*)
        FROM users
        WHERE premium_until IS NOT NULL AND premium_until > ?
        """,
        (now_ts,),
    )
    (count,) = cur.fetchone()
    conn.close()
    return int(count or 0)


def get_usage_stats() -> Dict[str, int]:
    conn = _get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            COALESCE(SUM(total_messages), 0) AS total_messages,
            COALESCE(SUM(total_tokens), 0) AS total_tokens
        FROM users
        """
    )
    row = cur.fetchone()
    conn.close()
    return {
        "total_messages": int(row["total_messages"]),
        "total_tokens": int(row["total_tokens"]),
    }

# === Projects (мульти-режимы бота по пользователю) ===
import time
import sqlite3

# Флаг, чтобы не создавать таблицы каждый раз
_PROJECTS_INITIALIZED = False


def _projects_conn() -> sqlite3.Connection:
    """
    Отдельный коннект для работы с проектами.
    Используем тот же файл БД (DB_PATH), что и для подписок.
    """
    from .subscription_db import DB_PATH  # если DB_PATH объявлен выше в этом файле

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_projects_schema() -> None:
    """
    Ленивая инициализация схемы для проектов.
    Таблицы создаются при первом обращении.
    """
    global _PROJECTS_INITIALIZED
    if _PROJECTS_INITIALIZED:
        return

    conn = _projects_conn()
    cur = conn.cursor()

    # Основная таблица проектов
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id     INTEGER NOT NULL,
            title           TEXT NOT NULL,
            description     TEXT DEFAULT '',
            system_prompt   TEXT DEFAULT '',
            is_archived     INTEGER NOT NULL DEFAULT 0,
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL
        );
        """
    )

    # Активный проект у пользователя
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_projects_state (
            telegram_id         INTEGER PRIMARY KEY,
            current_project_id  INTEGER,
            updated_at          INTEGER NOT NULL
        );
        """
    )

    conn.commit()
    conn.close()
    _PROJECTS_INITIALIZED = True


def create_project(
    telegram_id: int,
    title: str,
    description: str = "",
    system_prompt: str = "",
) -> dict:
    """
    Создать проект для пользователя.
    Возвращает dict с данными проекта.
    """
    _ensure_projects_schema()
    conn = _projects_conn()
    ts = int(time.time())

    cur = conn.execute(
        """
        INSERT INTO projects (telegram_id, title, description, system_prompt, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (telegram_id, title.strip(), description.strip(), system_prompt.strip(), ts, ts),
    )
    project_id = cur.lastrowid

    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    conn.commit()
    conn.close()

    return dict(row) if row else {}


def list_projects(telegram_id: int, include_archived: bool = False) -> list[dict]:
    """
    Получить список проектов пользователя.
    """
    _ensure_projects_schema()
    conn = _projects_conn()
    if include_archived:
        rows = conn.execute(
            "SELECT * FROM projects WHERE telegram_id = ? ORDER BY is_archived, updated_at DESC",
            (telegram_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM projects WHERE telegram_id = ? AND is_archived = 0 ORDER BY updated_at DESC",
            (telegram_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_project(telegram_id: int, project_id: int) -> dict | None:
    """
    Получить конкретный проект по id (проверяем, что принадлежит пользователю).
    """
    _ensure_projects_schema()
    conn = _projects_conn()
    row = conn.execute(
        """
        SELECT * FROM projects
        WHERE id = ? AND telegram_id = ?
        """,
        (project_id, telegram_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_current_project(telegram_id: int, project_id: int | None) -> None:
    """
    Установить активный проект для пользователя.
    project_id = None -> сбросить (работа без проекта).
    """
    _ensure_projects_schema()
    conn = _projects_conn()
    ts = int(time.time())
    conn.execute(
        """
        INSERT INTO user_projects_state (telegram_id, current_project_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            current_project_id = excluded.current_project_id,
            updated_at         = excluded.updated_at
        """,
        (telegram_id, project_id, ts),
    )
    conn.commit()
    conn.close()


def get_current_project(telegram_id: int) -> dict | None:
    """
    Вернёт активный проект пользователя (или None, если не выбран).
    """
    _ensure_projects_schema()
    conn = _projects_conn()
    state = conn.execute(
        "SELECT current_project_id FROM user_projects_state WHERE telegram_id = ?",
        (telegram_id,),
    ).fetchone()

    if not state or state["current_project_id"] is None:
        conn.close()
        return None

    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?",
        (state["current_project_id"],),
    ).fetchone()
    conn.close()

    return dict(row) if row else None


def archive_project(telegram_id: int, project_id: int) -> None:
    """
    Пометить проект как архивный (не показываем в основном списке).
    """
    _ensure_projects_schema()
    conn = _projects_conn()
    ts = int(time.time())
    conn.execute(
        """
        UPDATE projects
        SET is_archived = 1,
            updated_at   = ?
        WHERE id = ? AND telegram_id = ?
        """,
        (ts, project_id, telegram_id),
    )

    # Если архивируем активный — снимаем его в состоянии
    conn.execute(
        """
        UPDATE user_projects_state
        SET current_project_id = NULL,
            updated_at         = ?
        WHERE telegram_id = ? AND current_project_id = ?
        """,
        (ts, telegram_id, project_id),
    )

    conn.commit()
    conn.close()
