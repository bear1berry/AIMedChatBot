from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
import threading

# База лежит в корне проекта как subscriptions.sqlite3
DB_PATH = (Path(__file__).resolve().parent.parent / "subscriptions.sqlite3").as_posix()

logger = logging.getLogger(__name__)
_lock = threading.Lock()

# Сколько бесплатных сообщений доступно без оплаты
FREE_MESSAGES_LIMIT = 3
# На сколько дней продляется премиум за один успешный платёж
PREMIUM_DAYS_PER_PAYMENT = 30


@dataclass
class User:
    id: int
    telegram_id: int
    username: Optional[str]
    is_admin: bool
    free_messages_used: int
    premium_until: Optional[datetime]
    created_at: datetime


@dataclass
class Payment:
    id: int
    telegram_id: int
    invoice_id: str
    amount: float
    currency: str
    status: str
    created_at: datetime
    paid_at: Optional[datetime]
    raw_payload: Optional[str]


def _connect() -> sqlite3.Connection:
    """Создать соединение с БД."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Создать / мигрировать схему БД, если нужно."""
    cur = conn.cursor()

    # Таблица пользователей
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL UNIQUE,
            username TEXT,
            is_admin INTEGER NOT NULL DEFAULT 0,
            free_messages_used INTEGER NOT NULL DEFAULT 0,
            premium_until TEXT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

    # Таблица платежей
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            invoice_id TEXT NOT NULL UNIQUE,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            paid_at TEXT NULL,
            raw_payload TEXT,
            FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
        )
        """
    )

    # На всякий случай — утилита для добавления колонок (если в будущем расширишь)
    def ensure_column(table: str, name: str, ddl: str) -> None:
        cur.execute(f"PRAGMA table_info({table})")
        cols = [row[1] for row in cur.fetchall()]
        if name not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # Здесь уже всё создано правильным CREATE, но оставляю пример:
    # ensure_column("users", "is_admin", "INTEGER NOT NULL DEFAULT 0")
    # ensure_column("users", "free_messages_used", "INTEGER NOT NULL DEFAULT 0")
    # ensure_column("users", "premium_until", "TEXT NULL")
    ensure_column("payments", "raw_payload", "TEXT")

    conn.commit()


def init_subscription_storage() -> None:
    """Инициализация БД (вызывается при старте бота)."""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
        finally:
            conn.close()
    logger.info("Subscription storage initialized")


# ---- ВАЖНО ----
# Алиас для старого кода, который импортирует init_db
def init_db() -> None:
    init_subscription_storage()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _row_to_user(row: sqlite3.Row) -> User:
    premium_until = _parse_dt(row["premium_until"])
    created_at = _parse_dt(row["created_at"]) or datetime.utcnow()
    return User(
        id=row["id"],
        telegram_id=row["telegram_id"],
        username=row["username"],
        is_admin=bool(row["is_admin"]),
        free_messages_used=row["free_messages_used"],
        premium_until=premium_until,
        created_at=created_at,
    )


def _row_to_payment(row: sqlite3.Row) -> Payment:
    created_at = _parse_dt(row["created_at"]) or datetime.utcnow()
    paid_at = _parse_dt(row["paid_at"])
    return Payment(
        id=row["id"],
        telegram_id=row["telegram_id"],
        invoice_id=row["invoice_id"],
        amount=row["amount"],
        currency=row["currency"],
        status=row["status"],
        created_at=created_at,
        paid_at=paid_at,
        raw_payload=row["raw_payload"],
    )


# --------------------
#   USERS
# --------------------

def get_or_create_user(telegram_id: int, username: Optional[str] = None) -> User:
    """Получить пользователя или создать, если его нет."""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()

            cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()
            if row:
                user = _row_to_user(row)
                # Обновим username, если изменился
                if username and user.username != username:
                    cur.execute(
                        "UPDATE users SET username = ? WHERE telegram_id = ?",
                        (username, telegram_id),
                    )
                    conn.commit()
                    user.username = username
                return user

            # Создаём нового
            cur.execute(
                "INSERT INTO users (telegram_id, username) VALUES (?, ?)",
                (telegram_id, username),
            )
            conn.commit()
            cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()
            return _row_to_user(row)
        finally:
            conn.close()


def get_user(telegram_id: int) -> Optional[User]:
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()
            return _row_to_user(row) if row else None
        finally:
            conn.close()


def mark_admin(telegram_id: int, username: Optional[str] = None) -> User:
    """Отметить пользователя как администратора (или создать его как админа)."""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()

            cur.execute(
                """
                UPDATE users
                SET is_admin = 1,
                    username = COALESCE(?, username)
                WHERE telegram_id = ?
                """,
                (username, telegram_id),
            )

            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO users (telegram_id, username, is_admin)
                    VALUES (?, ?, 1)
                    """,
                    (telegram_id, username),
                )

            conn.commit()
            cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            return _row_to_user(cur.fetchone())
        finally:
            conn.close()


def increment_free_usage(telegram_id: int) -> None:
    """Увеличить счётчик бесплатных использований для пользователя."""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET free_messages_used = free_messages_used + 1 WHERE telegram_id = ?",
                (telegram_id,),
            )
            conn.commit()
        finally:
            conn.close()


def set_premium(telegram_id: int, days: int = PREMIUM_DAYS_PER_PAYMENT) -> None:
    """Выдать / продлить премиум-подписку пользователю."""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()

            cur.execute(
                "SELECT premium_until FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = cur.fetchone()

            now = datetime.utcnow()
            base = now
            if row and row["premium_until"]:
                current = _parse_dt(row["premium_until"])
                if current and current > now:
                    base = current

            new_until = base + timedelta(days=days)

            cur.execute(
                "UPDATE users SET premium_until = ? WHERE telegram_id = ?",
                (new_until.isoformat(), telegram_id),
            )
            conn.commit()
        finally:
            conn.close()


def user_has_access(user: User) -> bool:
    """
    Правила доступа:
    - админ — всегда True
    - если премиум активен — True
    - иначе пока free_messages_used < FREE_MESSAGES_LIMIT — True
    """
    if user.is_admin:
        return True

    now = datetime.utcnow()
    if user.premium_until and user.premium_until > now:
        return True

    if user.free_messages_used < FREE_MESSAGES_LIMIT:
        return True

    return False


# --------------------
#   PAYMENTS
# --------------------

def create_payment(
    telegram_id: int,
    invoice_id: str,
    amount: float,
    currency: str,
    status: str,
    raw_payload: Optional[str] = None,
) -> Payment:
    """Создать запись о платеже."""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO payments (telegram_id, invoice_id, amount, currency, status, raw_payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, invoice_id, amount, currency, status, raw_payload),
            )
            conn.commit()

            cur.execute("SELECT * FROM payments WHERE invoice_id = ?", (invoice_id,))
            row = cur.fetchone()
            return _row_to_payment(row)
        finally:
            conn.close()


def update_payment_status(invoice_id: str, status: str, mark_paid: bool = False) -> None:
    """
    Обновить статус платежа.
    Если mark_paid=True — ставим paid_at и продлеваем премиум пользователю.
    """
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()

            if mark_paid:
                now = datetime.utcnow().isoformat()
                cur.execute(
                    "UPDATE payments SET status = ?, paid_at = ? WHERE invoice_id = ?",
                    (status, now, invoice_id),
                )
            else:
                cur.execute(
                    "UPDATE payments SET status = ? WHERE invoice_id = ?",
                    (status, invoice_id),
                )

            conn.commit()

            if mark_paid:
                cur.execute(
                    "SELECT telegram_id FROM payments WHERE invoice_id = ?",
                    (invoice_id,),
                )
                row = cur.fetchone()
                if row:
                    set_premium(row["telegram_id"])
        finally:
            conn.close()


def get_pending_payments(limit: int = 100) -> List[Payment]:
    """
    Получить список платежей, которые ещё не завершены:
    статусы 'pending' или 'active' — для периодической проверки через API.
    """
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT *
                FROM payments
                WHERE status IN ('pending', 'active')
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [_row_to_payment(r) for r in rows]
        finally:
            conn.close()


def get_user_payments(telegram_id: int, limit: int = 20) -> List[Payment]:
    """История платежей пользователя."""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT *
                FROM payments
                WHERE telegram_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (telegram_id, limit),
            )
            rows = cur.fetchall()
            return [_row_to_payment(r) for r in rows]
        finally:
            conn.close()


def get_admin_stats() -> Dict[str, Any]:
    """Агрегированная статистика для админ-панели."""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()

            # всего пользователей
            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]

            # админы
            cur.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1")
            admins = cur.fetchone()[0]

            # активные премиумы
            now = datetime.utcnow().isoformat()
            cur.execute(
                """
                SELECT COUNT(*)
                FROM users
                WHERE premium_until IS NOT NULL AND premium_until > ?
                """,
                (now,),
            )
            active_premium = cur.fetchone()[0]

            # всего платежей
            cur.execute("SELECT COUNT(*) FROM payments")
            total_payments = cur.fetchone()[0]

            # выручка по успешным платежам
            cur.execute(
                """
                SELECT SUM(amount)
                FROM payments
                WHERE status IN ('paid', 'finished')
                """
            )
            total_revenue = cur.fetchone()[0] or 0.0

            return {
                "total_users": total_users,
                "admins": admins,
                "active_premium": active_premium,
                "total_payments": total_payments,
                "total_revenue": float(total_revenue),
            }
        finally:
            conn.close()
