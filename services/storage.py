from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bot.config import (
    USERS_FILE_PATH,
    DEFAULT_DAILY_LIMIT,
    PLAN_BASIC,
    PLAN_PREMIUM,
    REF_BONUS_PER_USER,
    SUBSCRIPTION_TARIFFS,
    DEFAULT_MODE,
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class UserRecord:
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None

    mode: str = DEFAULT_MODE

    plan: str = PLAN_BASIC
    premium_until: Optional[str] = None  # ISO8601 UTC

    daily_date: str = field(default_factory=lambda: date.today().isoformat())
    daily_used: int = 0
    total_used: int = 0

    ref_code: Optional[str] = None
    referred_by: Optional[int] = None
    ref_count: int = 0
    ref_bonus_messages: int = 0

     history: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)

    pending_invoice_id: Optional[int] = None
    pending_invoice_tariff: Optional[str] = None

    created_at: str = field(default_factory=lambda: _now_utc().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserRecord":
        # Простая миграция старых записей
        if "history" not in data:
            data["history"] = {}
        if "ref_bonus_messages" not in data:
            data["ref_bonus_messages"] = 0
        if "plan" not in data:
            data["plan"] = PLAN_BASIC
        if "mode" not in data:
            data["mode"] = DEFAULT_MODE
        return cls(**data)


class Storage:␊
    """
    Простое JSON-хранилище пользователей.

    Формат файла: { "<user_id>": { ...UserRecord... }, ... }
    """

    def __init__(self, path: Path = USERS_FILE_PATH) -> None:
        self.path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    # --- low-level I/O ---

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._data = raw
            else:
                self._data = {}
        except Exception:
@@ -143,55 +143,93 @@ class Storage:
            self._save()
            return self._data[key], False

        # Создаём нового
        rec = UserRecord(user_id=user_id)
        if tg_user is not None:
            rec.username = getattr(tg_user, "username", None)
            rec.first_name = getattr(tg_user, "first_name", None)
            rec.last_name = getattr(tg_user, "last_name", None)

        self._ensure_ref_code(rec)
        self._data[key] = rec.to_dict()
        self._save()
        return self._data[key], True

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        return self._data.get(self._key(user_id))

    def save_user(self, user_dict: Dict[str, Any]) -> None:
        key = self._key(user_dict["user_id"])
        self._data[key] = user_dict
        self._save()

    # --- режимы ---

    def set_mode(self, user_id: int, mode: str) -> Dict[str, Any]:
        rec_dict, _ = self.get_or_create_user(user_id)
        rec_dict["mode"] = mode
        self.save_user(rec_dict)
        return rec_dict

    def get_mode(self, user_id: int) -> str:
        rec_dict, _ = self.get_or_create_user(user_id)
        return rec_dict.get("mode") or DEFAULT_MODE

    # --- публичные резюме по пользователю ---

    def get_plan(self, user_id: int) -> Dict[str, Any]:
        rec_dict, _ = self.get_or_create_user(user_id)
        premium_until = rec_dict.get("premium_until")
        return {
            "code": rec_dict.get("plan") or PLAN_BASIC,
            "premium_until": premium_until,
            "is_premium_active": self.is_premium_active(rec_dict),
        }

    def get_limits(self, user_id: int) -> Dict[str, Any]:
        rec_dict, _ = self.get_or_create_user(user_id)
        daily_limit = self.get_daily_limit(rec_dict)
        used_today = int(rec_dict.get("daily_used") or 0)
        remaining = None if daily_limit is None else max(daily_limit - used_today, 0)
        return {
            "daily_limit": daily_limit,
            "used_today": used_today,
            "remaining_daily": remaining,
            "total_used": int(rec_dict.get("total_used") or 0),
            "is_premium": self.is_premium_active(rec_dict),
            "premium_until": rec_dict.get("premium_until"),
        }

    def get_referral_stats(self, user_id: int) -> Dict[str, Any]:
        rec_dict, _ = self.get_or_create_user(user_id)
        return {
            "ref_count": int(rec_dict.get("ref_count") or 0),
            "ref_bonus_messages": int(rec_dict.get("ref_bonus_messages") or 0),
            "ref_code": rec_dict.get("ref_code"),
            "referred_by": rec_dict.get("referred_by"),
        }

    # --- лимиты / планы ---

    def is_premium_active(self, user_dict: Dict[str, Any]) -> bool:
        until_str = user_dict.get("premium_until")
        if not until_str:
            return False
        try:
            until = datetime.fromisoformat(until_str)
        except Exception:
            return False
        return until > _now_utc()

    def get_daily_limit(self, user_dict: Dict[str, Any]) -> Optional[int]:
        """
        Возвращает лимит сообщений на день.
        None = безлимит (например для Premium).
        """
        if self.is_premium_active(user_dict):
            return None
        base = DEFAULT_DAILY_LIMIT
        bonus = int(user_dict.get("ref_bonus_messages") or 0)
        return base + bonus

    def register_usage(self, user_dict: Dict[str, Any], count: int = 1) -> Dict[str, Any]:
@@ -238,47 +276,81 @@ class Storage:

        # Обновляем нового
        new_rec["referred_by"] = referrer["user_id"]
        self.save_user(new_rec)

        # Обновляем реферера
        referrer["ref_count"] = int(referrer.get("ref_count") or 0) + 1
        referrer["ref_bonus_messages"] = int(referrer.get("ref_bonus_messages") or 0) + REF_BONUS_PER_USER
        self.save_user(referrer)

        return referrer, new_rec

    # --- история диалогов ---

    def add_history_message(self, user_dict: Dict[str, Any], mode: str, role: str, content: str, max_len: int = 20) -> Dict[str, Any]:
        history = user_dict.setdefault("history", {})
        conv = history.setdefault(mode, [])
        conv.append({"role": role, "content": content})
        if len(conv) > max_len:
            # Обрезаем старые сообщения
            conv[:] = conv[-max_len:]
        user_dict["history"] = history
        self.save_user(user_dict)
        return user_dict

    def get_history(self, user_dict: Dict[str, Any], mode: str, max_len: int = 20) -> List[Dict[str, str]]:
        history = user_dict.get("history") or {}
        conv = history.get(mode) or []
        if len(conv) > max_len:
            return conv[-max_len:]
        return conv

    def get_chat_history(self, user_id: int, mode: Optional[str] = None, max_len: int = 20) -> List[Dict[str, str]]:
        rec_dict, _ = self.get_or_create_user(user_id)
        mode_to_use = mode or rec_dict.get("mode") or DEFAULT_MODE
        return self.get_history(rec_dict, mode_to_use, max_len=max_len)

    def append_chat_history(
        self, user_id: int, role: str, content: str, mode: Optional[str] = None
    ) -> Dict[str, Any]:
        rec_dict, _ = self.get_or_create_user(user_id)
        mode_to_use = mode or rec_dict.get("mode") or DEFAULT_MODE
        return self.add_history_message(rec_dict, mode_to_use, role, content)

    def update_on_request(
        self, user_id: int, mode: str, user_prompt: Optional[str] = None
    ) -> Dict[str, Any]:
        rec_dict, _ = self.get_or_create_user(user_id)
        rec_dict["daily_used"] = int(rec_dict.get("daily_used") or 0) + 1
        rec_dict["total_used"] = int(rec_dict.get("total_used") or 0) + 1
        if user_prompt:
            rec_dict = self.add_history_message(rec_dict, mode, "user", user_prompt)
        self.save_user(rec_dict)
        return rec_dict


    # --- инвойсы ---

    def set_pending_invoice(self, user_id: int, invoice_id: int, tariff_code: str) -> Dict[str, Any]:
        rec_dict, _ = self.get_or_create_user(user_id)
        rec_dict["pending_invoice_id"] = int(invoice_id)
        rec_dict["pending_invoice_tariff"] = tariff_code
        self.save_user(rec_dict)
        return rec_dict

    def clear_pending_invoice(self, user_id: int) -> Dict[str, Any]:
        rec_dict, _ = self.get_or_create_user(user_id)
        rec_dict["pending_invoice_id"] = None
        rec_dict["pending_invoice_tariff"] = None
        self.save_user(rec_dict)
        return rec_dict


_storage: Optional[Storage] = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage
