from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, List, Dict

# Путь к базе подписок
DB_PATH = os.getenv(
    "SUBSCRIPTION_DB_PATH",
    os.path.join(os.path.dirname(__file__), "subscriptions.db"),
)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """
    Инициализация БД: создаём таблицы, если их нет,
    и аккуратно добавляем недостающие колонки.
    """
    conn = _get_conn()
    cur = conn.cursor()

    # Таблица пользователей
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE,
            username TEXT,
            free_requests_used INTEGER NOT NULL DEFAULT 0,
            free_tokens_used INTEGER NOT NULL DEFAULT 0,
            paid_until INTEGER,
            current_mode TEXT NOT NULL DEFAULT 'default',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )

    # Таблица платежей
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            invoice_id INTEGER NOT NULL UNIQUE,
            currency TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            paid_at INTEGER,
            raw_json TEXT
        )
        """
    )

    # На случай старой схемы — докидываем недостающие колонки
    def ensure_column(table: str, column: str, ddl: str) -> None:
        cur.execute(f"PRAGMA table_info({table})")
        cols = [r[1] for r in cur.fetchall()]
        if column not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    # users
    ensure_column(
        "users",
        "free_requests_used",
        "free_requests_used INTEGER NOT NULL DEFAULT 0",
    )
    ensure_column(
        "users",
        "free_tokens_used",
        "free_tokens_used INTEGER NOT NULL DEFAULT 0",
    )
    ensure_column("users", "paid_until", "paid_until INTEGER")
    ensure_column(
        "users",
        "current_mode",
        "current_mode TEXT NOT NULL DEFAULT 'default'",
    )
    ensure_column(
        "users",
        "created_at",
        "created_at INTEGER NOT NULL DEFAULT strftime('%s','now')",
    )
    ensure_column(
        "users",
        "updated_at",
        "updated_at INTEGER NOT NULL DEFAULT strftime('%s','now')",
    )

    # payments
    ensure_column("payments", "paid_at", "paid_at INTEGER")
    ensure_column("payments", "raw_json", "raw_json TEXT")

    conn.commit()
    conn.close()


@dataclass
class User:
    id: int
    telegram_id: int
    username: Optional[str]
    free_requests_used: int
    free_tokens_used: int
    paid_until: Optional[int]
    current_mode: str
    created_at: int
    updated_at: int

    @property
    def has_active_subscription(self) -> bool:
        if not self.paid_until:
            return False
        return self.paid_until >= int(time.time())


@dataclass
class Payment:
    id: int
    telegram_id: int
    invoice_id: int
    currency: str
    amount: float
    status: str
    created_at: int
    paid_at: Optional[int]
    raw_json: Optional[str]


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        telegram_id=row["telegram_id"],
        username=row["username"],
        free_requests_used=row["free_requests_used"],
        free_tokens_used=row["free_tokens_used"],
        paid_until=row["paid_until"],
        current_mode=row["current_mode"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_payment(row: sqlite3.Row) -> Payment:
    return Payment(
        id=row["id"],
        telegram_id=row["telegram_id"],
        invoice_id=row["invoice_id"],
        currency=row["currency"],
        amount=row["amount"],
        status=row["status"],
        created_at=row["created_at"],
        paid_at=row["paid_at"],
        raw_json=row["raw_json"],
    )


def get_or_create_user(telegram_id: int, username: Optional[str]) -> User:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    now = int(time.time())

    if row is None:
        cur.execute(
            """
            INSERT INTO users (
                telegram_id,
                username,
                free_requests_used,
                free_tokens_used,
                paid_until,
                current_mode,
                created_at,
                updated_at
            )
            VALUES (?, ?, 0, 0, NULL, 'default', ?, ?)
            """,
            (telegram_id, username, now, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
    else:
        # Обновляем username, если поменялся
        if username and row["username"] != username:
            cur.execute(
                "UPDATE users SET username = ?, updated_at = ? WHERE telegram_id = ?",
                (username, now, telegram_id),
            )
            conn.commit()
            cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()

    conn.close()
    return _row_to_user(row)


def get_user(telegram_id: int) -> Optional[User]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return _row_to_user(row)


def update_usage(telegram_id: int, add_requests: int, add_tokens: int) -> None:
    """
    Обновляем счётчики бесплатных запросов/токенов.
    """
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        """
        UPDATE users
        SET
            free_requests_used = free_requests_used + ?,
            free_tokens_used = free_tokens_used + ?,
            updated_at = ?
        WHERE telegram_id = ?
        """,
        (max(0, add_requests), max(0, add_tokens), now, telegram_id),
    )
    conn.commit()
    conn.close()


def grant_subscription(telegram_id: int, days: int = 30) -> None:
    """
    Выдать/продлить подписку на days дней.
    Если уже активна — продлеваем от текущей даты окончания.
    """
    conn = _get_conn()
    cur = conn.cursor()

    now_ts = int(time.time())
    cur.execute("SELECT paid_until FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    if row and row["paid_until"]:
        base = max(now_ts, int(row["paid_until"]))
    else:
        base = now_ts

    new_paid_until = base + days * 24 * 60 * 60

    cur.execute(
        """
        UPDATE users
        SET paid_until = ?, updated_at = ?
        WHERE telegram_id = ?
        """,
        (new_paid_until, now_ts, telegram_id),
    )
    conn.commit()
    conn.close()


def set_user_mode(telegram_id: int, mode_key: str) -> None:
    """
    Смена режима (стиля) бота для конкретного пользователя.
    """
    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())
    cur.execute(
        """
        UPDATE users
        SET current_mode = ?, updated_at = ?
        WHERE telegram_id = ?
        """,
        (mode_key, now, telegram_id),
    )
    conn.commit()
    conn.close()


def create_payment(
    telegram_id: int,
    invoice_id: int,
    currency: str,
    amount: float,
    status: str,
    created_at: int,
    paid_at: Optional[int] = None,
    raw_json: Optional[str] = None,
) -> None:
    """
    Создаём или обновляем платёж (invoice_id уникален).
    """
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO payments (
            telegram_id,
            invoice_id,
            currency,
            amount,
            status,
            created_at,
            paid_at,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(invoice_id) DO UPDATE SET
            telegram_id = excluded.telegram_id,
            currency   = excluded.currency,
            amount     = excluded.amount,
            status     = excluded.status,
            created_at = excluded.created_at,
            paid_at    = excluded.paid_at,
            raw_json   = excluded.raw_json
        """,
        (telegram_id, invoice_id, currency, amount, status, created_at, paid_at, raw_json),
    )
    conn.commit()
    conn.close()


def mark_payment_paid(invoice_id: int, paid_at: Optional[int] = None) -> None:
    """
    Помечаем платёж как оплаченный и выдаём подписку (30 дней).
    """
    if paid_at is None:
        paid_at = int(time.time())

    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM payments WHERE invoice_id = ?", (invoice_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return

    payment = _row_to_payment(row)

    # Обновляем статус / paid_at
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

    # Выдаём подписку пользователю
    grant_subscription(payment.telegram_id, days=30)


def get_stats() -> Dict[str, int]:
    """
    Статистика для /admin.
    """
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]

    now_ts = int(time.time())
    cur.execute(
        "SELECT COUNT(*) FROM users WHERE paid_until IS NOT NULL AND paid_until >= ?",
        (now_ts,),
    )
    active_premium_users = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM payments WHERE status = 'paid'")
    total_paid_invoices = cur.fetchone()[0]

    conn.close()

    return {
        "total_users": int(total_users),
        "active_premium_users": int(active_premium_users),
        "total_paid_invoices": int(total_paid_invoices),
    }


def get_premium_users(limit: int = 50) -> List[User]:
    conn = _get_conn()
    cur = conn.cursor()
    now_ts = int(time.time())
    cur.execute(
        """
        SELECT * FROM users
        WHERE paid_until IS NOT NULL AND paid_until >= ?
        ORDER BY paid_until ASC
        LIMIT ?
        """,
        (now_ts, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [_row_to_user(r) for r in rows]


def get_recent_payments(limit: int = 30) -> List[Payment]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM payments ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return [_row_to_payment(r) for r in rows]


def export_users_and_payments(max_rows: int = 500) -> str:
    """
    Возвращает CSV-подобный текст для /admin export.
    Можно сразу кидать в Excel / Google Sheets.
    """
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users ORDER BY id DESC LIMIT ?", (max_rows,))
    users_rows = cur.fetchall()

    cur.execute("SELECT * FROM payments ORDER BY id DESC LIMIT ?", (max_rows,))
    pay_rows = cur.fetchall()

    conn.close()

    lines: List[str] = []

    # --- Пользователи ---
    lines.append(
        "kind,user_id,telegram_id,username,free_requests_used,free_tokens_used,paid_until_ts,paid_until_iso,"
        "current_mode,created_at_ts,created_at_iso,updated_at_ts,updated_at_iso"
    )

    for r in users_rows:
        paid_until_ts = r["paid_until"] or 0
        paid_until_iso = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(paid_until_ts))
            if paid_until_ts
            else ""
        )
        created_ts = r["created_at"]
        updated_ts = r["updated_at"]
        created_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(created_ts))
        updated_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(updated_ts))

        username = (r["username"] or "").replace(",", " ")

        lines.append(
            ",".join(
                [
                    "user",
                    str(r["id"]),
                    str(r["telegram_id"]),
                    username,
                    str(r["free_requests_used"]),
                    str(r["free_tokens_used"]),
                    str(paid_until_ts),
                    paid_until_iso,
                    (r["current_mode"] or "").replace(",", " "),
                    str(created_ts),
                    created_iso,
                    str(updated_ts),
                    updated_iso,
                ]
            )
        )

    # Пустая строка-разделитель
    lines.append("")

    # --- Платежи ---
    lines.append(
        "kind,payment_id,telegram_id,invoice_id,currency,amount,status,created_at_ts,"
        "created_at_iso,paid_at_ts,paid_at_iso"
    )

    for r in pay_rows:
        created_ts = r["created_at"]
        created_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(created_ts))

        paid_at_ts = r["paid_at"] or 0
        paid_at_iso = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(paid_at_ts))
            if paid_at_ts
            else ""
        )

        lines.append(
            ",".join(
                [
                    "payment",
                    str(r["id"]),
                    str(r["telegram_id"]),
                    str(r["invoice_id"]),
                    (r["currency"] or "").replace(",", " "),
                    str(r["amount"]),
                    (r["status"] or "").replace(",", " "),
                    str(created_ts),
                    created_iso,
                    str(paid_at_ts),
                    paid_at_iso,
                ]
            )
        )

    return "\n".join(lines)
