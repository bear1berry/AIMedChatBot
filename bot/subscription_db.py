from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("SUBSCRIPTION_DB_PATH", "subscription.db")


# ---------------------------------------------------------------------------
# Базовые helpers
# ---------------------------------------------------------------------------


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Инициализация БД
# ---------------------------------------------------------------------------


def init_db() -> None:
    """Создаём таблицы, если их ещё нет."""

    conn = _get_conn()
    cur = conn.cursor()

    # Пользователи
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id       INTEGER NOT NULL UNIQUE,
            username          TEXT,
            full_name         TEXT,
            is_admin          INTEGER DEFAULT 0,

            mode              TEXT DEFAULT 'universal',
            note              TEXT,

            free_usage_date   TEXT,
            free_usage_count  INTEGER DEFAULT 0,

            premium_until     INTEGER,             -- unixtime до какого момента premium

            referral_code     TEXT UNIQUE,

            created_at        INTEGER,
            updated_at        INTEGER
        );
        """
    )

    # Реферальные связи
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_telegram_id   INTEGER NOT NULL,
            referred_telegram_id   INTEGER NOT NULL,
            created_at             INTEGER,
            UNIQUE(referrer_telegram_id, referred_telegram_id)
        );
        """
    )

    # Инвойсы (крипто-оплата)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id    TEXT NOT NULL UNIQUE,
            telegram_id   INTEGER NOT NULL,
            plan_code     TEXT NOT NULL,
            amount_usdt   REAL,
            period_days   INTEGER,
            pay_url       TEXT,
            status        TEXT DEFAULT 'created',
            created_at    INTEGER,
            paid_at       INTEGER
        );
        """
    )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Пользователь
# ---------------------------------------------------------------------------


def get_or_create_user(
    telegram_id: int,
    username: str | None = None,
    full_name: str | None = None,
    is_admin: bool = False,
) -> Dict[str, Any]:
    """Возвращает существующего пользователя или создаёт нового."""

    now = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    if row:
        # Обновим базовые поля (юзернейм мог поменяться)
        cur.execute(
            """
            UPDATE users
               SET username = COALESCE(?, username),
                   full_name = COALESCE(?, full_name),
                   is_admin = CASE WHEN ? THEN 1 ELSE is_admin END,
                   updated_at = ?
             WHERE telegram_id = ?
            """,
            (username, full_name, is_admin, now, telegram_id),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
        conn.close()
        return dict(row)

    cur.execute(
        """
        INSERT INTO users (
            telegram_id, username, full_name, is_admin,
            mode, free_usage_date, free_usage_count,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'universal', NULL, 0, ?, ?)
        """,
        (telegram_id, username, full_name, 1 if is_admin else 0, now, now),
    )
    conn.commit()

    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row)


def get_user_by_telegram_id(telegram_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return _dict(row)


# ---------------------------------------------------------------------------
# Режимы, досье
# ---------------------------------------------------------------------------


def set_user_mode(telegram_id: int, mode: str) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "UPDATE users SET mode = ?, updated_at = ? WHERE telegram_id = ?",
        (mode, now, telegram_id),
    )
    if cur.rowcount == 0:
        # На всякий случай создадим пользователя
        get_or_create_user(telegram_id)
        cur.execute(
            "UPDATE users SET mode = ?, updated_at = ? WHERE telegram_id = ?",
            (mode, now, telegram_id),
        )
    conn.commit()
    conn.close()


def set_user_note(telegram_id: int, note: str) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        "UPDATE users SET note = ?, updated_at = ? WHERE telegram_id = ?",
        (note, now, telegram_id),
    )
    if cur.rowcount == 0:
        get_or_create_user(telegram_id)
        cur.execute(
            "UPDATE users SET note = ?, updated_at = ? WHERE telegram_id = ?",
            (note, now, telegram_id),
        )
    conn.commit()
    conn.close()


def get_user_note(telegram_id: int) -> Optional[str]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT note FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return row["note"] if row and row["note"] is not None else None


# ---------------------------------------------------------------------------
# Бесплатный лимит
# ---------------------------------------------------------------------------


def _today_str() -> str:
    return date.today().isoformat()


def get_free_usage_today(telegram_id: int) -> int:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT free_usage_date, free_usage_count FROM users WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row:
        return 0

    if row["free_usage_date"] == _today_str():
        return int(row["free_usage_count"] or 0)
    return 0


def increment_usage(telegram_id: int) -> int:
    """Инкремент счётчика бесплатных сообщений за сегодня и возвращает новое значение."""

    today = _today_str()
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT free_usage_date, free_usage_count FROM users WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = cur.fetchone()

    if not row:
        # Создаём пользователя, если его ещё нет
        get_or_create_user(telegram_id)
        cur.execute(
            "SELECT free_usage_date, free_usage_count FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()

    if row["free_usage_date"] == today:
        new_count = int(row["free_usage_count"] or 0) + 1
    else:
        new_count = 1

    now = int(time.time())
    cur.execute(
        """
        UPDATE users
           SET free_usage_date = ?,
               free_usage_count = ?,
               updated_at = ?
         WHERE telegram_id = ?
        """,
        (today, new_count, now, telegram_id),
    )
    conn.commit()
    conn.close()
    return new_count


# ---------------------------------------------------------------------------
# Premium
# ---------------------------------------------------------------------------


def has_premium(telegram_id: int) -> bool:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT premium_until FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return False

    premium_until = row["premium_until"]
    if premium_until is None:
        return False

    return int(premium_until) > int(time.time())


def grant_premium_days(telegram_id: int, days: int) -> None:
    """Добавляет/продлевает Premium на указанное количество дней."""

    seconds = days * 24 * 3600
    now_ts = int(time.time())

    conn = _get_conn()
    cur = conn.cursor()

    # убеждаемся, что пользователь есть
    get_or_create_user(telegram_id)

    cur.execute("SELECT premium_until FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    current = int(row["premium_until"]) if row and row["premium_until"] else 0
    base = max(current, now_ts)
    new_until = base + seconds

    cur.execute(
        "UPDATE users SET premium_until = ?, updated_at = ? WHERE telegram_id = ?",
        (new_until, now_ts, telegram_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Реферальная система
# ---------------------------------------------------------------------------


def ensure_referral_code(telegram_id: int) -> str:
    """
    Возвращает реферальный код пользователя, при отсутствии — создаёт.
    Формат кода делаем простой и уникальный.
    """

    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT referral_code FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    if row and row["referral_code"]:
        code = row["referral_code"]
        conn.close()
        return code

    # создаём пользователя на всякий случай
    get_or_create_user(telegram_id)

    # простой уникальный код: BBX + telegram_id
    code = f"BBX{telegram_id}"

    # если вдруг занято (теоретически) — добавим счётчик
    suffix = 1
    while True:
        try_code = code if suffix == 1 else f"{code}_{suffix}"
        cur.execute(
            "SELECT 1 FROM users WHERE referral_code = ?",
            (try_code,),
        )
        exists = cur.fetchone()
        if not exists:
            code = try_code
            break
        suffix += 1

    now = int(time.time())
    cur.execute(
        "UPDATE users SET referral_code = ?, updated_at = ? WHERE telegram_id = ?",
        (code, now, telegram_id),
    )
    conn.commit()
    conn.close()
    return code


def find_user_by_referral_code(code: str) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE referral_code = ?", (code,))
    row = cur.fetchone()
    conn.close()
    return _dict(row)


def add_referral(referrer_telegram_id: int, referred_telegram_id: int) -> bool:
    """
    Добавляет запись о реферале.
    Возвращает True, если запись новая; False, если такая пара уже была.
    """

    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())

    try:
        cur.execute(
            """
            INSERT INTO referrals (referrer_telegram_id, referred_telegram_id, created_at)
            VALUES (?, ?, ?)
            """,
            (referrer_telegram_id, referred_telegram_id, now),
        )
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        # уже есть такая пара (UNIQUE)
        success = False
    finally:
        conn.close()

    return success


def get_user_referrals(telegram_id: int) -> List[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT u.*
          FROM referrals r
          JOIN users u
            ON u.telegram_id = r.referred_telegram_id
         WHERE r.referrer_telegram_id = ?
         ORDER BY r.created_at DESC
        """,
        (telegram_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Инвойсы (крипто-оплаты)
# ---------------------------------------------------------------------------


def create_invoice_record(
    invoice_id: str,
    telegram_id: int,
    plan_code: str,
    amount_usdt: float,
    period_days: int,
    pay_url: Optional[str] = None,
) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())

    cur.execute(
        """
        INSERT OR REPLACE INTO invoices (
            invoice_id, telegram_id, plan_code, amount_usdt,
            period_days, pay_url, status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, COALESCE(
                    (SELECT status FROM invoices WHERE invoice_id = ?),
                    'created'
               ), COALESCE(
                    (SELECT created_at FROM invoices WHERE invoice_id = ?),
                    ?
               ))
        """,
        (
            invoice_id,
            telegram_id,
            plan_code,
            amount_usdt,
            period_days,
            pay_url,
            invoice_id,
            invoice_id,
            now,
        ),
    )
    conn.commit()
    conn.close()


def get_last_invoice_for_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
          FROM invoices
         WHERE telegram_id = ?
         ORDER BY id DESC
         LIMIT 1
        """,
        (telegram_id,),
    )
    row = cur.fetchone()
    conn.close()
    return _dict(row)


def mark_invoice_paid(invoice_id: str) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        """
        UPDATE invoices
           SET status = 'paid',
               paid_at = COALESCE(paid_at, ?)
         WHERE invoice_id = ?
        """,
        (now, invoice_id),
    )
    conn.commit()
    conn.close()
