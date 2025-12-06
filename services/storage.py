# services/storage.py
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiogram.types import User as TgUser

from bot.config import (
    USERS_FILE_PATH,
    DEFAULT_MODE_KEY,
    ADMIN_IDS,
)

logger = logging.getLogger(__name__)


# ====== Проекты / устойчивые темы диалога ======


@dataclass
class ProjectRecord:
    """
    Устойчивый «проект» / тема пользователя.

    Примеры:
    - «Telegram-бот BlackBox»
    - «Отношения с девушкой»
    - «Форма и тренировки»
    """

    id: str
    title: str
    description: str = ""
    tags: List[str] = field(default_factory=list)
    last_used_ts: float = field(default_factory=lambda: time.time())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectRecord":
        return cls(
            id=str(data.get("id", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            tags=list(data.get("tags") or []),
            last_used_ts=float(data.get("last_used_ts", time.time())),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ====== Пользователь ======


@dataclass
class UserRecord:
    id: int

    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None

    # активный режим (универсальный / медицина / коуч / бизнес / креатив)
    mode_key: str = DEFAULT_MODE_KEY

    # тариф
    plan_type: str = "basic"  # basic | premium
    premium_until: Optional[float] = None

    # лимиты по запросам
    daily_used: int = 0
    monthly_used: int = 0
    last_daily_reset: Optional[str] = None  # YYYY-MM-DD
    last_monthly_reset: Optional[str] = None  # YYYY-MM

    # суммарная статистика
    total_requests: int = 0
    total_tokens: int = 0

    # реферальная система
    ref_code: Optional[str] = None
    referred_by: Optional[str] = None
    referrals_count: int = 0

    # подписка / оплаты
    last_invoice_id: Optional[int] = None
    last_tariff_key: Optional[str] = None

    # стиль и профиль
    style_hint: Optional[str] = None  # общий стиль общения пользователя
    profile: Dict[str, Any] = field(default_factory=dict)  # «досье»: привычки, цели
    projects: Dict[str, Any] = field(default_factory=dict)  # id -> ProjectRecord dict

    created_at: float = field(default_factory=lambda: time.time())
    updated_at: float = field(default_factory=lambda: time.time())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserRecord":
        """
        Аккуратно восстанавливаем запись, игнорируя лишние поля из старых версий.
        Всем новым полям даём дефолты.
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
            last_tariff_key=data.get("last_tariff_key") or data.get("last_invoice_code"),
            style_hint=data.get("style_hint"),
            profile=data.get("profile") or {},
            projects=data.get("projects") or {},
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ====== Хранилище ======


class Storage:
    """
    Простое файловое хранилище в JSON.

    Схема (новая):
      {
        "<user_id>": { ... UserRecord.to_dict() ... },
        "<user_id2>": { ... }
      }

    При загрузке понимает и старую схему вида {"users": {...}} и мигрирует.
    """

    def __init__(self, path: Path = USERS_FILE_PATH):
        self.path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    # --- low-level ---

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return

        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)

            if isinstance(raw, dict) and "users" in raw and isinstance(
                raw.get("users"), dict
            ):
                # старая схема: {"users": {...}}
                self._data = raw["users"]
            elif isinstance(raw, dict):
                self._data = raw
            else:
                logger.warning("Unexpected users.json format, resetting")
                self._data = {}
        except Exception as e:
            logger.exception("Failed to load users.json: %s", e)
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
        except Exception as e:
            logger.exception("Failed to save users.json: %s", e)

    def _key(self, user_id: int) -> str:
        return str(user_id)

    # --- helpers ---

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

    def _ensure_profile_projects_dicts(self, rec: UserRecord) -> None:
        if not isinstance(rec.profile, dict):
            rec.profile = {}
        if not isinstance(rec.projects, dict):
            rec.projects = {}

    # --- public API: базовый пользователь / тарифы / лимиты ---

    def get_or_create_user(
        self, user_id: int, tg_user: Optional[TgUser] = None
    ) -> Tuple[UserRecord, bool]:
        key = self._key(user_id)
        created = False

        if key in self._data:
            try:
                rec = UserRecord.from_dict(self._data[key])
            except Exception:
                logger.exception("Failed to restore user %s, recreating", key)
                rec = UserRecord(id=user_id)
                created = True
        else:
            rec = UserRecord(id=user_id)
            created = True

        # Обновляем базовые поля
        if tg_user:
            rec.username = tg_user.username
            rec.first_name = tg_user.first_name
            rec.last_name = tg_user.last_name

        self._ensure_profile_projects_dicts(rec)
        self._ensure_ref_code(rec)
        self._refresh_plan_from_premium(rec)
        self._refresh_limits_dates(rec)
        rec.updated_at = time.time()

        self._data[key] = rec.to_dict()
        self._save()
        return rec, created

    def save_user(self, rec: UserRecord) -> None:
        key = self._key(rec.id)
        self._ensure_profile_projects_dicts(rec)
        self._ensure_ref_code(rec)
        self._refresh_plan_from_premium(rec)
        self._refresh_limits_dates(rec)
        rec.updated_at = time.time()
        self._data[key] = rec.to_dict()
        self._save()

    def set_mode(self, user_id: int, mode_key: str) -> None:
        key = self._key(user_id)
        if key not in self._data:
            rec = UserRecord(id=user_id, mode_key=mode_key)
        else:
            rec = UserRecord.from_dict(self._data[key])
            rec.mode_key = mode_key
        self.save_user(rec)

    def apply_usage(self, rec: UserRecord, tokens: int) -> None:
        rec.total_requests += 1
        rec.total_tokens += max(0, tokens)
        rec.daily_used += 1
        rec.monthly_used += 1
        self.save_user(rec)

    def is_admin(self, user_id: int) -> bool:
        return user_id in ADMIN_IDS

    def effective_plan(self, rec: UserRecord, is_admin: bool) -> str:
        if is_admin:
            return "admin"
        self._refresh_plan_from_premium(rec)
        return rec.plan_type or "basic"

    # --- public API: рефералка ---

    def apply_referral(self, new_user_id: int, ref_code: str) -> None:
        # находим владельца ref_code
        owner_id: Optional[int] = None
        for key, value in self._data.items():
            try:
                if value.get("ref_code") == ref_code:
                    owner_id = int(key)
                    break
            except Exception:
                continue

        if not owner_id:
            return

        # обновляем владельца
        owner_rec = UserRecord.from_dict(self._data[str(owner_id)])
        owner_rec.referrals_count += 1
        self.save_user(owner_rec)

        # отмечаем у нового пользователя, что он пришёл по рефке
        new_key = self._key(new_user_id)
        if new_key in self._data:
            rec = UserRecord.from_dict(self._data[new_key])
        else:
            rec = UserRecord(id=new_user_id)
        rec.referred_by = ref_code
        self.save_user(rec)

    # --- public API: подписка / оплаты ---

    def store_invoice(self, rec: UserRecord, invoice_id: int, tariff_key: str) -> None:
        rec.last_invoice_id = invoice_id
        rec.last_tariff_key = tariff_key
        self.save_user(rec)

    def get_last_invoice(
        self, rec: UserRecord
    ) -> Tuple[Optional[int], Optional[str]]:
        return rec.last_invoice_id, rec.last_tariff_key

    def activate_premium(self, rec: UserRecord, months: int) -> None:
        now = time.time()
        base = rec.premium_until if rec.premium_until and rec.premium_until > now else now
        # грубо считаем месяц как 30 дней
        rec.premium_until = base + 30 * 24 * 3600 * months
        rec.plan_type = "premium"
        self.save_user(rec)

    # --- public API: профиль / стиль ---

    def get_profile(self, user_id: int) -> Dict[str, Any]:
        """
        Возвращает копию профиля пользователя (чтобы снаружи его не портили напрямую).
        """
        rec, _ = self.get_or_create_user(user_id)
        if not isinstance(rec.profile, dict):
            rec.profile = {}
            self.save_user(rec)
        return dict(rec.profile)

    def update_profile(self, user_id: int, fields: Dict[str, Any]) -> None:
        """
        Мягкое обновление профиля: просто мержим словари.
        """
        rec, _ = self.get_or_create_user(user_id)
        if not isinstance(rec.profile, dict):
            rec.profile = {}
        rec.profile.update(fields)
        self.save_user(rec)

    def update_style_hint(self, user_id: int, style_hint: Optional[str]) -> None:
        """
        Обновление текстовой подсказки по стилю общения пользователя.
        """
        rec, _ = self.get_or_create_user(user_id)
        rec.style_hint = style_hint
        self.save_user(rec)

    # --- public API: проекты / рабочие темы ---

    def get_projects(self, user_id: int) -> Dict[str, ProjectRecord]:
        """
        Возвращает словарь {project_id: ProjectRecord}.
        """
        rec, _ = self.get_or_create_user(user_id)
        self._ensure_profile_projects_dicts(rec)

        projects: Dict[str, ProjectRecord] = {}
        raw = rec.projects or {}

        if isinstance(raw, dict):
            for pid, pdata in raw.items():
                try:
                    pdata_dict = dict(pdata or {})
                    pdata_dict.setdefault("id", pid)
                    proj = ProjectRecord.from_dict(pdata_dict)
                    projects[pid] = proj
                except Exception:
                    logger.exception(
                        "Failed to restore project %s for user %s", pid, rec.id
                    )

        return projects

    def upsert_project(self, user_id: int, project: ProjectRecord) -> None:
        """
        Добавить/обновить один проект.
        """
        rec, _ = self.get_or_create_user(user_id)
        self._ensure_profile_projects_dicts(rec)
        rec.projects[project.id] = project.to_dict()
        self.save_user(rec)

    def replace_projects(
        self, user_id: int, projects: Dict[str, ProjectRecord]
    ) -> None:
        """
        Полностью заменить список проектов (удобно для LLM-снэпшота).
        """
        rec, _ = self.get_or_create_user(user_id)
        self._ensure_profile_projects_dicts(rec)
        rec.projects = {pid: p.to_dict() for pid, p in projects.items()}
        self.save_user(rec)

    def delete_project(self, user_id: int, project_id: str) -> None:
        rec, _ = self.get_or_create_user(user_id)
        self._ensure_profile_projects_dicts(rec)
        if project_id in rec.projects:
            rec.projects.pop(project_id, None)
            self.save_user(rec)

    def apply_projects_snapshot(
        self, user_id: int, snapshot: List[Dict[str, Any]]
    ) -> None:
        """
        Обновление проектов по снэпшоту от LLM.

        Ожидаемый формат элемента snapshot:
        {
          "id": "blackbox_bot",
          "title": "Telegram-бот BlackBox",
          "description": "...",
          "tags": ["бот", "telegram", "проект"]
        }
        """
        projects: Dict[str, ProjectRecord] = {}

        for item in snapshot:
            if not item:
                continue
            pid = str(item.get("id") or item.get("title") or "").strip()
            if not pid:
                continue
            data = dict(item)
            data["id"] = pid
            try:
                proj = ProjectRecord.from_dict(data)
            except Exception:
                logger.exception("Failed to build ProjectRecord from %s", item)
                continue
            projects[pid] = proj

        self.replace_projects(user_id, projects)
