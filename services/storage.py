from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from aiogram.types import User as TgUser

from bot.config import (
    USERS_FILE_PATH,
    DEFAULT_MODE_KEY,
    ADMIN_IDS,
)

logger = logging.getLogger(__name__)

# SQLite вместо "опасного JSON"
DB_PATH: Path = USERS_FILE_PATH.with_suffix(".db")


@dataclass
class UserRecord:
    id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None

    mode_key: str = DEFAULT_MODE_KEY

    # Тариф
    plan_type: str = "basic"  # basic | premium
    premium_until: Optional[float] = None

    # Лимиты
    daily_used: int = 0
    monthly_used: int = 0
    last_daily_reset: Optional[str] = None  # YYYY-MM-DD
    last_monthly_reset: Optional[str] = None  # YYYY-MM

    # Статистика
    total_requests: int = 0
    total_tokens: int = 0

    # Рефералка
    ref_code: Optional[str] = None
    referred_by: Optional[str] = None
    referrals_count: int = 0

    # Оплата / подписка
    last_invoice_id: Optional[int] = None
    last_tariff_key: Optional[str] = None

    # Стиль общения (под твой Style Engine)
    style_hint: Optional[str] = None

    created_at: float = field(default_factory=lambda: time.time())
    updated_at: float = field(default_factory=lambda: time.time())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserRecord":
        """
        Аккуратно восстанавливаем запись, игнорируя лишние поля
        из старых версий (для миграции из JSON).
        """
        return cls(
            id=int(data.get("id")),
            username=data.get("username"),
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            mode_key=data.get("mode_key", DEFAULT_MODE_KEY),
            plan_type=data.get("plan_type", "basic"),
            premium_until=data.get("premium_until"),
            daily_used=int(data.get("daily_used", 0)),
            monthly_used=int(data.get("monthly_used", 0)),
            last_daily_reset=data.get("last_daily_reset"),
            last_monthly_reset=data.get("last_monthly_reset"),
            total_requests=int(data.get("total_requests", 0)),
            total_tokens=int(data.get("total_tokens", 0)),
            ref_code=data.get("ref_code"),
            referred_by=data.get("referred_by"),
            referrals_count=int(data.get("referrals_count", 0)),
            last_invoice_id=data.get("last_invoice_id"),
            # поддержка старого имени поля, если оно было
            last_tariff_key=data.get("last_tariff_key")
            or data.get("last_invoice_code"),
            style_hint=data.get("style_hint"),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "UserRecord":
        """Восстановление записи из строки БД."""
        return cls(
            id=row["id"],
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            mode_key=row["mode_key"],
            plan_type=row["plan_type"],
            premium_until=row["premium_until"],
            daily_used=row["daily_used"],
            monthly_used=row["monthly_used"],
            last_daily_reset=row["last_daily_reset"],
            last_monthly_reset=row["last_monthly_reset"],
            total_requests=row["total_requests"],
            total_tokens=row["total_tokens"],
            ref_code=row["ref_code"],
            referred_by=row["referred_by"],
            referrals_count=row["referrals_count"],
            last_invoice_id=row["last_invoice_id"],
            last_tariff_key=row["last_tariff_key"],
            style_hint=row["style_hint"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Storage:
    """
    Нормальное хранилище на SQLite.

    Таблицы:
    - users            — профиль и тариф пользователя
    - messages         — вся история диалогов (для аналитики/памяти)
    - projects         — устойчивые темы/проекты (под 2.1)
    - payments         — истории платежей
    - referrals        — лог рефералок
    - daily_summaries  — дневные саммари (под ежедневные отчёты)
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or DB_PATH
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        self._maybe_migrate_from_json()

    # ------------------------------------------------------------------
    # Инициализация схемы БД
    # ------------------------------------------------------------------
    def _init_db(self) -> None:
        cur = self._conn.cursor()

        # users
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id                INTEGER PRIMARY KEY,
                username          TEXT,
                first_name        TEXT,
                last_name         TEXT,
                mode_key          TEXT NOT NULL,
                plan_type         TEXT NOT NULL,
                premium_until     REAL,

                daily_used        INTEGER NOT NULL DEFAULT 0,
                monthly_used      INTEGER NOT NULL DEFAULT 0,
                last_daily_reset  TEXT,
                last_monthly_reset TEXT,

                total_requests    INTEGER NOT NULL DEFAULT 0,
                total_tokens      INTEGER NOT NULL DEFAULT 0,

                ref_code          TEXT,
                referred_by       TEXT,
                referrals_count   INTEGER NOT NULL DEFAULT 0,

                last_invoice_id   INTEGER,
                last_tariff_key   TEXT,

                style_hint        TEXT,

                created_at        REAL NOT NULL,
                updated_at        REAL NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_ref_code
            ON users(ref_code) WHERE ref_code IS NOT NULL
            """
        )

        # messages
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                role       TEXT NOT NULL,   -- 'user' | 'assistant' | 'system'
                content    TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id)"
        )

        # projects (под будущие ProjectRecord / 2.1)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                title        TEXT NOT NULL,
                description  TEXT,
                tags         TEXT,
                last_used_ts REAL NOT NULL,
                created_at   REAL NOT NULL,
                UNIQUE(user_id, title),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        # payments
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                invoice_id  INTEGER NOT NULL,
                tariff_key  TEXT NOT NULL,
                status      TEXT NOT NULL,   -- 'pending' | 'paid' | 'canceled'
                amount_usdt TEXT,
                created_at  REAL NOT NULL,
                paid_at     REAL,
                UNIQUE(invoice_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)"
        )

        # referrals (под аналитику реф-системы)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id          INTEGER NOT NULL,
                referred_user_id  INTEGER NOT NULL,
                ref_code          TEXT NOT NULL,
                created_at        REAL NOT NULL,
                FOREIGN KEY(owner_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(referred_user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_referrals_owner ON referrals(owner_id)"
        )

        # daily_summaries (для дневных отчётов)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_summaries (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                date       TEXT NOT NULL,  -- YYYY-MM-DD
                summary    TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(user_id, date),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )

        self._conn.commit()

    # ------------------------------------------------------------------
    # Миграция из старого JSON (users.json) → SQLite
    # ------------------------------------------------------------------
    def _maybe_migrate_from_json(self) -> None:
        """Мягкая миграция: если БД пустая, а users.json есть — переливаем."""
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT 1 FROM users LIMIT 1")
            row = cur.fetchone()
            if row is not None:
                # уже есть данные в БД — ничего не трогаем
                return

            if not USERS_FILE_PATH.exists():
                return

            logger.info("Migrating users from JSON (%s) to SQLite (%s)",
                        USERS_FILE_PATH, self.db_path)

            with USERS_FILE_PATH.open("r", encoding="utf-8") as f:
                raw = json.load(f)

            if not isinstance(raw, dict):
                logger.warning(
                    "Unexpected users.json format (not dict), skipping migration"
                )
                return

            now = time.time()
            for key, value in raw.items():
                try:
                    rec = UserRecord.from_dict(value)
                except Exception:
                    logger.exception("Failed to restore user from JSON: %s", key)
                    continue

                # Если created_at не было — ставим что-то адекватное
                if not rec.created_at:
                    rec.created_at = now
                if not rec.updated_at:
                    rec.updated_at = now

                self._upsert_user(rec, commit=False)

            self._conn.commit()
            logger.info("Migration from JSON completed successfully")

        except Exception as e:
            logger.exception("Migration from JSON failed: %s", e)

    # ------------------------------------------------------------------
    # Внутренние вспомогательные методы
    # ------------------------------------------------------------------
    def _ensure_ref_code(self, rec: UserRecord) -> None:
        if not rec.ref_code:
            # Простая, но устойчивая схема
            rec.ref_code = f"u{rec.id}"

    def _refresh_plan_from_premium(self, rec: UserRecord) -> None:
        if rec.plan_type == "premium" and rec.premium_until is not None:
            if time.time() > rec.premium_until:
                rec.plan_type = "basic"

    def _refresh_limits_dates(self, rec: UserRecord) -> None:
        now = time.localtime()
        day_key = f"{now.tm_year:04d}-{now.tm_mon:02d}-{now.tm_mday:02d}"
        month_key = f"{now.tm_year:04d}-{now.tm_mon:02d}"

        if rec.last_daily_reset != day_key:
            rec.daily_used = 0
            rec.last_daily_reset = day_key

        if rec.last_monthly_reset != month_key:
            rec.monthly_used = 0
            rec.last_monthly_reset = month_key

    def _upsert_user(self, rec: UserRecord, commit: bool = True) -> None:
        self._ensure_ref_code(rec)
        self._refresh_plan_from_premium(rec)
        self._refresh_limits_dates(rec)
        rec.updated_at = time.time()

        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO users (
                id, username, first_name, last_name,
                mode_key,
                plan_type, premium_until,
                daily_used, monthly_used, last_daily_reset, last_monthly_reset,
                total_requests, total_tokens,
                ref_code, referred_by, referrals_count,
                last_invoice_id, last_tariff_key,
                style_hint,
                created_at, updated_at
            )
            VALUES (
                :id, :username, :first_name, :last_name,
                :mode_key,
                :plan_type, :premium_until,
                :daily_used, :monthly_used, :last_daily_reset, :last_monthly_reset,
                :total_requests, :total_tokens,
                :ref_code, :referred_by, :referrals_count,
                :last_invoice_id, :last_tariff_key,
                :style_hint,
                :created_at, :updated_at
            )
            ON CONFLICT(id) DO UPDATE SET
                username          = excluded.username,
                first_name        = excluded.first_name,
                last_name         = excluded.last_name,
                mode_key          = excluded.mode_key,
                plan_type         = excluded.plan_type,
                premium_until     = excluded.premium_until,
                daily_used        = excluded.daily_used,
                monthly_used      = excluded.monthly_used,
                last_daily_reset  = excluded.last_daily_reset,
                last_monthly_reset= excluded.last_monthly_reset,
                total_requests    = excluded.total_requests,
                total_tokens      = excluded.total_tokens,
                ref_code          = excluded.ref_code,
                referred_by       = excluded.referred_by,
                referrals_count   = excluded.referrals_count,
                last_invoice_id   = excluded.last_invoice_id,
                last_tariff_key   = excluded.last_tariff_key,
                style_hint        = excluded.style_hint,
                created_at        = users.created_at,
                updated_at        = excluded.updated_at
            """,
            rec.to_dict(),
        )
        if commit:
            self._conn.commit()

    # ------------------------------------------------------------------
    # Публичный API — максимально совместим с твоей текущей логикой
    # ------------------------------------------------------------------
    def get_or_create_user(
        self, user_id: int, tg_user: Optional[TgUser] = None
    ) -> Tuple[UserRecord, bool]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()

        created = False
        if row is not None:
            rec = UserRecord.from_row(row)
        else:
            rec = UserRecord(id=user_id)
            created = True

        # Обновляем базовые поля из Telegram-профиля
        if tg_user:
            rec.username = tg_user.username
            rec.first_name = tg_user.first_name
            rec.last_name = tg_user.last_name

        self._upsert_user(rec)  # обновит ref_code, лимиты и т.д.
        return rec, created

    def save_user(self, rec: UserRecord) -> None:
        self._upsert_user(rec)

    def set_mode(self, user_id: int, mode_key: str) -> None:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()

        if row is not None:
            rec = UserRecord.from_row(row)
        else:
            rec = UserRecord(id=user_id)

        rec.mode_key = mode_key
        self._upsert_user(rec)

    def apply_usage(self, rec: UserRecord, tokens: int) -> None:
        rec.total_requests += 1
        rec.total_tokens += max(0, tokens)
        rec.daily_used += 1
        rec.monthly_used += 1
        self._upsert_user(rec)

    def is_admin(self, user_id: int) -> bool:
        return user_id in ADMIN_IDS

    def effective_plan(self, rec: UserRecord, is_admin: bool) -> str:
        if is_admin:
            return "admin"
        self._refresh_plan_from_premium(rec)
        return rec.plan_type or "basic"

    def apply_referral(self, new_user_id: int, ref_code: str) -> None:
        """
        Логика та же, но через SQLite + отдельная таблица referrals.
        """
        cur = self._conn.cursor()

        # Находим владельца рефкода
        cur.execute("SELECT * FROM users WHERE ref_code = ?", (ref_code,))
        owner_row = cur.fetchone()
        if owner_row is None:
            return

        owner_rec = UserRecord.from_row(owner_row)
        owner_rec.referrals_count += 1
        self._upsert_user(owner_rec, commit=False)

        # Новый пользователь
        new_rec, _ = self.get_or_create_user(new_user_id)
        if not new_rec.referred_by:
            new_rec.referred_by = ref_code
        self._upsert_user(new_rec, commit=False)

        # Лог в таблицу referrals
        now = time.time()
        cur.execute(
            """
            INSERT INTO referrals (owner_id, referred_user_id, ref_code, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (owner_rec.id, new_rec.id, ref_code, now),
        )

        self._conn.commit()

    def store_invoice(self, rec: UserRecord, invoice_id: int, tariff_key: str) -> None:
        """
        Сохраняем последний инвойс пользователя + логируем в payments.
        """
        rec.last_invoice_id = invoice_id
        rec.last_tariff_key = tariff_key
        self._upsert_user(rec, commit=False)

        # Лог в payments (status по умолчанию pending)
        now = time.time()
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO payments (
                user_id, invoice_id, tariff_key, status, created_at
            )
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (rec.id, invoice_id, tariff_key, now),
        )
        self._conn.commit()

    def get_last_invoice(self, rec: UserRecord) -> Tuple[Optional[int], Optional[str]]:
        return rec.last_invoice_id, rec.last_tariff_key

    def activate_premium(self, rec: UserRecord, months: int) -> None:
        now = time.time()
        base = rec.premium_until if rec.premium_until and rec.premium_until > now else now
        # грубо считаем месяц как 30 дней
        rec.premium_until = base + 30 * 24 * 3600 * months
        rec.plan_type = "premium"
        self._upsert_user(rec, commit=False)

        # Если есть последний инвойс — помечаем его как оплаченный
        if rec.last_invoice_id is not None:
            cur = self._conn.cursor()
            cur.execute(
                """
                UPDATE payments
                SET status = 'paid', paid_at = ?
                WHERE invoice_id = ?
                """,
                (time.time(), rec.last_invoice_id),
            )

        self._conn.commit()

    # ------------------------------------------------------------------
    # Доп. методы под будущее (messages / summaries) — пока просто база
    # ------------------------------------------------------------------
    def log_message(self, user_id: int, role: str, content: str) -> None:
        """Записать сообщение в историю (для аналитики/памяти)."""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO messages (user_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, role, content, time.time()),
        )
        self._conn.commit()

    def add_daily_summary(self, user_id: int, date_str: str, summary: str) -> None:
        """Сохранить/обновить дневной summary (под отчёты)."""
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO daily_summaries (user_id, date, summary, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET
                summary = excluded.summary,
                created_at = excluded.created_at
            """,
            (user_id, date_str, summary, time.time()),
        )
        self._conn.commit()
