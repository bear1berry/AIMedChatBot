# bot/subscription_db.py
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
import threading

# Файл БД лежит в корне проекта рядом с папкой bot
DB_PATH = (Path(__file__).resolve().parent.parent / "subscriptions.sqlite3").as_posix()

logger = logging.getLogger(__name__)
_lock = threading.Lock()

# Сколько бесплатных запросов есть у обычного пользователя
FREE_MESSAGES_LIMIT = 3
# Сколько дней добавляем к подписке за один оплаченный счёт
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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Создание / мягкая миграция таблиц.
    Вызывается при каждом обращении, поэтому можно безопасно обновлять схему.
    """
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
            premium_until TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP NULL,
            raw_payload TEXT,
            FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
        )
        """
    )

    # Мягкая миграция: если в старой БД не было нужных колонок — добавляем
    def ensure_column(table: str, name: str, ddl: str) -> None:
        cur.execute(f"PRAGMA table_info({table})")
        cols = [row[1] for row in cur.fetchall()]
        if name not in cols:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    ensure_column("users", "is_admin", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("users", "free_messages_used", "INTEGER NOT NULL DEFAULT 0")
    ensure_column("users", "premium_until", "TIMESTAMP NULL")
    ensure_column("payments", "raw_payload", "TEXT")

    conn.commit()


def init_subscription_storage() -> None:
    """
    Инициализация БД.
    Вызови один раз при старте приложения (это уже делается в main.py).
    """
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
        finally:
            conn.close()
    logger.info("Subscription storage initialized")


def _row_to_user(row: sqlite3.Row) -> User:
    premium_until = None
    if row["premium_until"]:
        # В SQLite TIMESTAMP обычно хранится как текст (ISO-строка)
        premium_until = datetime.fromisoformat(row["premium_until"])

    if isinstance(row["created_at"], str):
        created_at = datetime.fromisoformat(row["created_at"])
    else:
        created_at = datetime.utcnow()

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
    paid_at = None
    if row["paid_at"]:
        paid_at = datetime.fromisoformat(row["paid_at"])

    if isinstance(row["created_at"], str):
        created_at = datetime.fromisoformat(row["created_at"])
    else:
        created_at = datetime.utcnow()

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


def get_or_create_user(telegram_id: int, username: Optional[str] = None) -> User:
    """
    Получить пользователя по telegram_id.
    Если его нет — создать.
    """
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()

            cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cur.fetchone()
            if row:
                user = _row_to_user(row)
                # Обновляем username, если поменялся
                if username and user.username != username:
                    cur.execute(
                        "UPDATE users SET username = ? WHERE telegram_id = ?",
                        (username, telegram_id),
                    )
                    conn.commit()
                    user.username = username
                return user

            # Если пользователя нет — создаём
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


def mark_admin(telegram_id: int, username: Optional[str] = None) -> User:
    """
    Сделать пользователя админом (для /admin, расширенных лимитов и т.п.).
    """
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


def increment_free_usage(telegram_id: int) -> None:
    """
    Увеличивает счётчик использованных бесплатных запросов.
    Вызывается, когда не админ и не премиум делает запрос к ИИ.
    """
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
    """
    Продлевает пользователю премиум:
    - если премиум уже есть — добавляем дни к текущей дате окончания
    - если нет — считаем от текущего момента
    """
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

            if row and row[0]:
                current = datetime.fromisoformat(row[0])
                base = current if current > now else now
            else:
                base = now

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
    Есть ли у пользователя доступ к ИИ:
    - админ — всегда True
    - есть активный premium_until в будущем — True
    - иначе проверяем лимит бесплатных запросов
    """
    if user.is_admin:
        return True

    now = datetime.utcnow()

    if user.premium_until and user.premium_until > now:
        return True

    if user.free_messages_used < FREE_MESSAGES_LIMIT:
        return True

    return False


def create_payment(
    telegram_id: int,
    invoice_id: str,
    amount: float,
    currency: str,
    status: str,
    raw_payload: Optional[str] = None,
) -> Payment:
    """
    Создать запись о платеже (после успешного createInvoice в CryptoBot).
    """
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
    Обновить статус платежа. Если mark_paid=True — проставляем paid_at и активируем подписку.
    """
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()

            if mark_paid:
                cur.execute(
                    """
                    UPDATE payments
                    SET status = ?, paid_at = ?
                    WHERE invoice_id = ?
                    """,
                    (status, datetime.utcnow().isoformat(), invoice_id),
                )
            else:
                cur.execute(
                    "UPDATE payments SET status = ? WHERE invoice_id = ?",
                    (status, invoice_id),
                )
            conn.commit()

            if mark_paid:
                # Активируем премиум после успешной оплаты
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
    Вернуть список "ожидающих" платежей для проверки статусов через CryptoBot.

    Используем статусы 'pending' и 'active' — под эти значения
    ты можешь адаптировать логику в зависимости от реальных ответов API.
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
    """
    Последние платежи конкретного пользователя.
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
    """
    Краткая аналитика для админ-панели (/admin).
    """
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1")
            admins = cur.fetchone()[0]

            now_iso = datetime.utcnow().isoformat()
            cur.execute(
                """
                SELECT COUNT(*)
                FROM users
                WHERE premium_until IS NOT NULL AND premium_until > ?
                """,
                (now_iso,),
            )
            active_premium = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM payments")
            total_payments = cur.fetchone()[0]

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
