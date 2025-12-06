from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

logger = logging.getLogger(__name__)

# Путь к SQLite-базе
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "aimedbot.db"

# Реферальные бонусы (можно переопределить через переменные окружения)
REFERRAL_BONUS_DAYS = int(os.getenv("REFERRAL_BONUS_DAYS", "7"))       # сколько дней премиума за реферала
REFERRAL_VOICE_WEEKS = int(os.getenv("REFERRAL_VOICE_WEEKS", "1"))     # на будущее: голосовой коуч
# Бонус к лимиту сообщений за каждого реферала (используется в main.py через config, но оставим тут как инфо)
# REFERRAL_DAILY_BONUS читается в main.py из bot.config или через getattr


@dataclass
class UserRecord:
    id: int
    username: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    is_bot: bool = False

    mode_key: Optional[str] = None  # текущий режим ассистента
    plan_code: str = "free"         # базовый/вынужденный код тарифа (free/premium/admin и т.п.)

    premium_until: Optional[str] = None  # YYYY-MM-DD, до какой даты активен premium

    daily_used: int = 0
    monthly_used: int = 0
    total_requests: int = 0
    total_tokens: int = 0

    # технические поля для сброса лимитов
    daily_date: Optional[str] = None      # YYYY-MM-DD
    monthly_month: Optional[str] = None   # YYYY-MM

    # рефералька
    ref_code: Optional[str] = None
    referrals_count: int = 0
    referrer_user_id: Optional[int] = None

    # дополнительные данные по наградам за реферал (JSON-строка)
    referral_rewards: Optional[str] = None

    # последняя оплата
    last_invoice_id: Optional[int] = None
    last_tariff_key: Optional[str] = None

    # стилистика общения
    style_hint: Optional[str] = None

    # авто-дневник
    last_summary_date: Optional[str] = None  # YYYY-MM-DD

    created_at: float = 0.0
    updated_at: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "UserRecord":
        return cls(
            id=row["id"],
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            is_bot=bool(row["is_bot"]),
            mode_key=row["mode_key"],
            plan_code=row["plan_code"] or "free",
            premium_until=row["premium_until"],
            daily_used=row["daily_used"],
            monthly_used=row["monthly_used"],
            total_requests=row["total_requests"],
            total_tokens=row["total_tokens"],
            daily_date=row["daily_date"],
            monthly_month=row["monthly_month"],
            ref_code=row["ref_code"],
            referrals_count=row["referrals_count"],
            referrer_user_id=row["referrer_user_id"],
            referral_rewards=row["referral_rewards"],
            last_invoice_id=row["last_invoice_id"],
            last_tariff_key=row["last_tariff_key"],
            style_hint=row["style_hint"],
            last_summary_date=row["last_summary_date"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class Storage:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    # --------------- Базовая схема БД ---------------

    def _init_db(self) -> None:
        cur = self._conn.cursor()

        # Пользователи
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id               INTEGER PRIMARY KEY,
                username         TEXT,
                first_name       TEXT,
                last_name        TEXT,
                is_bot           INTEGER DEFAULT 0,

                mode_key         TEXT,
                plan_code        TEXT,

                premium_until    TEXT,

                daily_used       INTEGER DEFAULT 0,
                monthly_used     INTEGER DEFAULT 0,
                total_requests   INTEGER DEFAULT 0,
                total_tokens     INTEGER DEFAULT 0,

                daily_date       TEXT,
                monthly_month    TEXT,

                ref_code         TEXT UNIQUE,
                referrals_count  INTEGER DEFAULT 0,
                referrer_user_id INTEGER,

                referral_rewards TEXT,

                last_invoice_id  INTEGER,
                last_tariff_key  TEXT,

                style_hint       TEXT,
                last_summary_date TEXT,

                created_at       REAL,
                updated_at       REAL
            )
            """
        )

        # Лёгкая миграция: гарантируем наличие новых колонок в уже существующей БД
        cur.execute("PRAGMA table_info(users)")
        cols = [r["name"] for r in cur.fetchall()]
        if "last_summary_date" not in cols:
            try:
                cur.execute("ALTER TABLE users ADD COLUMN last_summary_date TEXT")
            except Exception:
                logger.exception("Failed to add last_summary_date column to users")
        if "referral_rewards" not in cols:
            try:
                cur.execute("ALTER TABLE users ADD COLUMN referral_rewards TEXT")
            except Exception:
                logger.exception("Failed to add referral_rewards column to users")

        # Сообщения
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                role       TEXT NOT NULL,  -- 'user' / 'assistant' / 'system'
                content    TEXT NOT NULL,
                created_at REAL NOT NULL,

                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_user_ts "
            "ON messages(user_id, created_at)"
        )

        # Дневные summary
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_summaries (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                date       TEXT NOT NULL,   -- YYYY-MM-DD
                summary    TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(user_id, date),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )

        # Проекты пользователя (слой проектов/тем)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                title        TEXT NOT NULL,
                description  TEXT,
                tags         TEXT,
                last_used_ts REAL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_user "
            "ON projects(user_id, last_used_ts)"
        )

        self._conn.commit()

    # --------------- Внутренние утилиты ---------------

    def _now_ts(self) -> float:
        return time.time()

    def _today_key(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    def _month_key(self) -> str:
        return time.strftime("%Y-%m", time.localtime())

    def _generate_ref_code(self, user_id: int) -> str:
        # простой детерминированный код, можно потом заменить на более сложный
        return f"BB{user_id}"

    def _fetch_user_row(self, user_id: int) -> Optional[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return cur.fetchone()

    def _upsert_user(self, user: UserRecord) -> None:
        cur = self._conn.cursor()
        now_ts = self._now_ts()

        if not user.created_at:
            user.created_at = now_ts
        user.updated_at = now_ts

        cur.execute(
            """
            INSERT INTO users (
                id,
                username, first_name, last_name, is_bot,
                mode_key, plan_code,
                premium_until,
                daily_used, monthly_used,
                total_requests, total_tokens,
                daily_date, monthly_month,
                ref_code, referrals_count, referrer_user_id,
                referral_rewards,
                last_invoice_id, last_tariff_key,
                style_hint,
                last_summary_date,
                created_at, updated_at
            )
            VALUES (
                :id,
                :username, :first_name, :last_name, :is_bot,
                :mode_key, :plan_code,
                :premium_until,
                :daily_used, :monthly_used,
                :total_requests, :total_tokens,
                :daily_date, :monthly_month,
                :ref_code, :referrals_count, :referrer_user_id,
                :referral_rewards,
                :last_invoice_id, :last_tariff_key,
                :style_hint,
                :last_summary_date,
                :created_at, :updated_at
            )
            ON CONFLICT(id) DO UPDATE SET
                username         = excluded.username,
                first_name       = excluded.first_name,
                last_name        = excluded.last_name,
                is_bot           = excluded.is_bot,
                mode_key         = excluded.mode_key,
                plan_code        = excluded.plan_code,
                premium_until    = excluded.premium_until,
                daily_used       = excluded.daily_used,
                monthly_used     = excluded.monthly_used,
                total_requests   = excluded.total_requests,
                total_tokens     = excluded.total_tokens,
                daily_date       = excluded.daily_date,
                monthly_month    = excluded.monthly_month,
                ref_code         = excluded.ref_code,
                referrals_count  = excluded.referrals_count,
                referrer_user_id = excluded.referrer_user_id,
                referral_rewards = excluded.referral_rewards,
                last_invoice_id  = excluded.last_invoice_id,
                last_tariff_key  = excluded.last_tariff_key,
                style_hint       = excluded.style_hint,
                last_summary_date = excluded.last_summary_date,
                updated_at       = excluded.updated_at
            """,
            {
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "is_bot": int(user.is_bot),
                "mode_key": user.mode_key,
                "plan_code": user.plan_code,
                "premium_until": user.premium_until,
                "daily_used": user.daily_used,
                "monthly_used": user.monthly_used,
                "total_requests": user.total_requests,
                "total_tokens": user.total_tokens,
                "daily_date": user.daily_date,
                "monthly_month": user.monthly_month,
                "ref_code": user.ref_code,
                "referrals_count": user.referrals_count,
                "referrer_user_id": user.referrer_user_id,
                "referral_rewards": user.referral_rewards,
                "last_invoice_id": user.last_invoice_id,
                "last_tariff_key": user.last_tariff_key,
                "style_hint": user.style_hint,
                "last_summary_date": user.last_summary_date,
                "created_at": user.created_at,
                "updated_at": user.updated_at,
            },
        )
        self._conn.commit()

    # --------------- Публичный API ---------------

    def get_or_create_user(self, user_id: int, tg_user) -> Tuple[UserRecord, bool]:
        """
        Возвращает (UserRecord, created)
        tg_user — объект aiogram.types.User (или любой с теми же полями).
        """
        row = self._fetch_user_row(user_id)
        created = False
        if row:
            user = UserRecord.from_row(row)
        else:
            created = True
            user = UserRecord(
                id=user_id,
                username=getattr(tg_user, "username", None),
                first_name=getattr(tg_user, "first_name", None),
                last_name=getattr(tg_user, "last_name", None),
                is_bot=bool(getattr(tg_user, "is_bot", False)),
                mode_key="universal",
                plan_code="free",
            )
            # ref_code генерируем сразу
            user.ref_code = self._generate_ref_code(user_id)
            self._upsert_user(user)

        # сброс дневных/месячных лимитов, если нужна дата/месяц
        today = self._today_key()
        month = self._month_key()
        changed = False

        if user.daily_date != today:
            user.daily_date = today
            user.daily_used = 0
            changed = True

        if user.monthly_month != month:
            user.monthly_month = month
            user.monthly_used = 0
            changed = True

        if changed:
            self._upsert_user(user)

        return user, created

    def save_user(self, user: UserRecord) -> None:
        self._upsert_user(user)

    # --- лимиты и план ---

    def effective_plan(self, user: UserRecord, is_admin: bool) -> str:
        """
        Возвращает фактический код плана:
        - 'admin'  если is_admin True
        - 'premium' если premium_until >= сегодня
        - иначе 'free'
        """
        if is_admin:
            return "admin"

        if user.premium_until:
            # premium_until в формате YYYY-MM-DD
            try:
                today = self._today_key()
                if user.premium_until >= today:
                    return "premium"
            except Exception:
                logger.debug("Invalid premium_until value: %r", user.premium_until)

        # fallback — план из поля, либо free
        return user.plan_code or "free"

    def apply_usage(self, user: UserRecord, tokens_used: int) -> None:
        """
        Обновляет счётчики использования.
        """
        user.total_requests += 1
        user.total_tokens += int(tokens_used or 0)

        # гарантируем актуальные дата/месяц
        today = self._today_key()
        month = self._month_key()
        if user.daily_date != today:
            user.daily_date = today
            user.daily_used = 0
        if user.monthly_month != month:
            user.monthly_month = month
            user.monthly_used = 0

        user.daily_used += 1
        user.monthly_used += 1

        self._upsert_user(user)

    # --- режимы ---

    def set_mode(self, user_id: int, mode_key: str) -> None:
        row = self._fetch_user_row(user_id)
        if not row:
            return
        user = UserRecord.from_row(row)
        user.mode_key = mode_key
        self._upsert_user(user)

    # --- логирование сообщений ---

    def log_message(self, user_id: int, role: str, content: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO messages (user_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, role, content, self._now_ts()),
        )
        self._conn.commit()

    # --- дневной дневник / summary ---

    def add_daily_summary(self, user_id: int, date_str: str, summary: str) -> None:
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT INTO daily_summaries (user_id, date, summary, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET
                summary = excluded.summary,
                created_at = excluded.created_at
            """,
            (user_id, date_str, summary, self._now_ts()),
        )
        self._conn.commit()

    def get_daily_summary(self, user_id: int, date_str: str) -> Optional[str]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT summary
            FROM daily_summaries
            WHERE user_id = ? AND date = ?
            """,
            (user_id, date_str),
        )
        row = cur.fetchone()
        if not row:
            return None
        return row["summary"]

    def get_messages_for_date(self, user_id: int, date_str: str) -> List[str]:
        # date_str: YYYY-MM-DD
        # считаем границы дня в timestamp
        try:
            dt_start = datetime.strptime(date_str, "%Y-%m-%d")
            dt_end = dt_start + timedelta(days=1)
            start_ts = dt_start.timestamp()
            end_ts = dt_end.timestamp()
        except Exception:
            # если вдруг формат странный — просто ничего не вернём
            return []

        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT content
            FROM messages
            WHERE user_id = ?
              AND created_at >= ?
              AND created_at < ?
              AND role = 'user'
            ORDER BY created_at ASC
            """,
            (user_id, start_ts, end_ts),
        )
        rows = cur.fetchall()
        return [r["content"] for r in rows]

    # --- вспомогательные функции по referral_rewards ---

    def _get_referral_rewards_dict(self, user: UserRecord) -> Dict[str, Any]:
        if not user.referral_rewards:
            return {}
        try:
            data = json.loads(user.referral_rewards)
            if isinstance(data, dict):
                return data
            return {}
        except Exception:
            return {}

    def _set_referral_rewards_dict(self, user: UserRecord, data: Dict[str, Any]) -> None:
        user.referral_rewards = json.dumps(data, ensure_ascii=False)

    # --- рефералка ---

    def apply_referral(self, new_user_id: int, ref_code: str) -> None:
        """
        Привязать нового пользователя к реф-коду, если такой есть.
        Начислить реферальные бонусы:
        - увеличить referrals_count у реферера;
        - записать referral_rewards;
        - опционально выдать дни премиума за реферала.
        """
        cur = self._conn.cursor()
        cur.execute(
            "SELECT * FROM users WHERE ref_code = ?",
            (ref_code,),
        )
        row = cur.fetchone()
        if not row:
            return

        referrer_id = row["id"]
        if referrer_id == new_user_id:
            return

        # обновляем счётчик у реферера
        referrer = UserRecord.from_row(row)
        referrer.referrals_count += 1

        rewards = self._get_referral_rewards_dict(referrer)
        rewards["referrals_total"] = referrer.referrals_count
        rewards["bonus_premium_days"] = rewards.get("bonus_premium_days", 0) + max(0, REFERRAL_BONUS_DAYS)
        rewards["bonus_voice_weeks"] = rewards.get("bonus_voice_weeks", 0) + max(0, REFERRAL_VOICE_WEEKS)
        self._set_referral_rewards_dict(referrer, rewards)

        # Начисляем премиум-дни за реферала (если >0)
        if REFERRAL_BONUS_DAYS > 0:
            self.add_premium_days(referrer, REFERRAL_BONUS_DAYS)
        else:
            self._upsert_user(referrer)

        # и сохраняем referrer_user_id у нового пользователя, если он уже есть
        row_new = self._fetch_user_row(new_user_id)
        if row_new:
            new_user = UserRecord.from_row(row_new)
            if not new_user.referrer_user_id:
                new_user.referrer_user_id = referrer_id
                self._upsert_user(new_user)

    # --- подписка и оплаты ---

    def store_invoice(self, user: UserRecord, invoice_id: int, tariff_key: str) -> None:
        user.last_invoice_id = int(invoice_id)
        user.last_tariff_key = tariff_key
        self._upsert_user(user)

    def get_last_invoice(self, user: UserRecord) -> Tuple[Optional[int], Optional[str]]:
        return user.last_invoice_id, user.last_tariff_key

    def add_premium_days(self, user: UserRecord, days: int) -> None:
        """
        Добавляет пользователю N дней премиума (используется тарифами и рефералкой).
        premium_until — YYYY-MM-DD
        """
        if days <= 0:
            # всё равно сохраним user (например, если только referral_rewards поменялись)
            self._upsert_user(user)
            return

        today = date.today()
        if user.premium_until:
            try:
                current = datetime.strptime(user.premium_until, "%Y-%m-%d").date()
            except Exception:
                current = today
        else:
            current = today

        base = max(today, current)
        new_date = base + timedelta(days=days)
        user.premium_until = new_date.strftime("%Y-%m-%d")
        if user.plan_code != "admin":
            user.plan_code = "premium"

        self._upsert_user(user)

    def activate_premium(self, user: UserRecord, months: int) -> None:
        """
        Активирует/продлевает premium на N месяцев.
        Реализация через add_premium_days (1 мес = 30 дней).
        """
        months = max(1, months)
        days = 30 * months
        self.add_premium_days(user, days)

    # --- админы ---

    def is_admin(self, user_id: int) -> bool:
        """
        Проверка админов через переменную окружения ADMIN_USER_IDS="1,2,3".
        Чтобы не тащить config и не создавать циклических импортов.
        """
        raw = os.getenv("ADMIN_USER_IDS", "")
        if not raw:
            return False
        try:
            ids = {int(x.strip()) for x in raw.split(",") if x.strip()}
        except ValueError:
            return False
        return user_id in ids
