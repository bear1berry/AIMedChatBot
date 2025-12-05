import json
import os
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Any, List, Tuple

# Аккуратно подтягиваем конфиг как модуль
try:
    import bot.config as config  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    config = None  # type: ignore[assignment]


# ===== КОНСТАНТЫ ИЗ CONFIG С ЗАПАСНЫМИ ЗНАЧЕНИЯМИ =====

DEFAULT_MODE_KEY: str = getattr(config, "DEFAULT_MODE_KEY", "universal")
REF_BONUS_PER_USER: int = int(getattr(config, "REF_BONUS_PER_USER", 5))
MAX_HISTORY_MESSAGES: int = int(getattr(config, "MAX_HISTORY_MESSAGES", 30))
PLAN_LIMITS: Dict[str, Dict[str, Any]] = getattr(
    config,
    "PLAN_LIMITS",
    {
        "free": {
            "title": "Free",
            "daily_base": 50,
            "description": "Базовый бесплатный тариф по умолчанию.",
        }
    },
)

# Путь к файлу users.json (не зависим от config.USERS_FILE_PATH)
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_USERS_FILE_PATH = str(DATA_DIR / "users.json")


def _today_str() -> str:
    """Сегодняшняя дата YYYY-MM-DD."""
    return date.today().isoformat()


class Storage:
    """Файловое JSON-хранилище пользователей, лимитов и рефералок."""

    def __init__(self, path: str | None = None) -> None:
        # Можно вызывать Storage() без аргументов
        self.path = path or DEFAULT_USERS_FILE_PATH
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.data: Dict[str, Any] = {"users": {}}
        self._load()

    # ===== ВНУТРЕННЕЕ =====

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                # Если файл битый — стартуем с пустой структурой
                self.data = {"users": {}}
        else:
            self._save()

    def _save(self) -> None:
        with self._lock:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    def _user_key(self, user_id: int) -> str:
        return str(user_id)

    def _ensure_user_structure(self, user: Dict[str, Any]) -> None:
        """Гарантируем, что у пользователя есть все нужные поля (для старых записей)."""
        user.setdefault("mode_key", DEFAULT_MODE_KEY)
        user.setdefault("plan", "free")
        user.setdefault(
            "dossier",
            {
                "messages_count": 0,
                "last_prompt_preview": "",
                "last_activity": None,
            },
        )
        user.setdefault(
            "referral",
            {
                "code": None,
                "invited_by": None,
                "invited_users": [],
                "total_requests": 0,
            },
        )
        user.setdefault("usage", {})    # {"YYYY-MM-DD": used_today}
        user.setdefault("history", [])  # список сообщений для LLM

    # ===== ПУБЛИЧНЫЙ API =====

    def get_or_create_user(self, user_id: int) -> Tuple[Dict[str, Any], bool]:
        """Возвращает (user_dict, created_flag). Совместимо с кодом, который это ожидает."""
        uid = self._user_key(user_id)
        if "users" not in self.data:
            self.data["users"] = {}

        users = self.data["users"]
        if uid in users:
            user = users[uid]
            created = False
        else:
            user = {}
            users[uid] = user
            created = True

        self._ensure_user_structure(user)
        self._save()
        return user, created

    def get_user(self, user_id: int) -> Dict[str, Any] | None:
        uid = self._user_key(user_id)
        user = self.data.get("users", {}).get(uid)
        if user is None:
            return None
        self._ensure_user_structure(user)
        return user

    def update_user_mode(self, user_id: int, mode_key: str) -> None:
        user, _ = self.get_or_create_user(user_id)
        user["mode_key"] = mode_key
        self._save()

    # ===== ДОСЬЕ =====

    def update_dossier_on_message(
        self,
        user_id: int,
        mode_key: str,
        user_prompt: str,
    ) -> None:
        """Обновляем «досье» при новом сообщении пользователя."""
        user, _ = self.get_or_create_user(user_id)
        dossier = user.setdefault("dossier", {})
        dossier["messages_count"] = int(dossier.get("messages_count", 0)) + 1
        dossier["last_prompt_preview"] = user_prompt[:120]
        dossier["last_activity"] = datetime.utcnow().isoformat()
        user["mode_key"] = mode_key
        self._save()

    # ===== ИСТОРИЯ ДИАЛОГА =====

    def get_history(self, user_id: int) -> List[Dict[str, str]]:
        user, _ = self.get_or_create_user(user_id)
        history = user.setdefault("history", [])
        if not isinstance(history, list):
            history = []
            user["history"] = history
            self._save()
        return history

    def append_history(self, user_id: int, role: str, content: str) -> None:
        """Добавляет сообщение в историю и режет до MAX_HISTORY_MESSAGES."""
        user, _ = self.get_or_create_user(user_id)
        history = user.setdefault("history", [])
        if not isinstance(history, list):
            history = []
            user["history"] = history

        history.append({"role": role, "content": content})
        if len(history) > MAX_HISTORY_MESSAGES:
            history[:] = history[-MAX_HISTORY_MESSAGES:]
        self._save()

    def reset_history(self, user_id: int) -> None:
        user, _ = self.get_or_create_user(user_id)
        user["history"] = []
        self._save()

    # ===== ПЛАН / ТАРИФ =====

    def get_plan(self, user_id: int) -> str:
        user, _ = self.get_or_create_user(user_id)
        return user.get("plan", "free")

    def set_plan(self, user_id: int, plan: str) -> None:
        if plan not in PLAN_LIMITS:
            return
        user, _ = self.get_or_create_user(user_id)
        user["plan"] = plan
        self._save()

    # ===== ЛИМИТЫ ==== #

    def register_request(self, user_id: int) -> None:
        """Увеличивает счётчики запросов: суточный + суммарный."""
        user, _ = self.get_or_create_user(user_id)
        referral = user.setdefault("referral", {})
        usage = user.setdefault("usage", {})

        referral["total_requests"] = int(referral.get("total_requests", 0)) + 1

        today = _today_str()
        usage[today] = int(usage.get(today, 0)) + 1

        self._save()

    def get_limits(self, user_id: int) -> Dict[str, Any]:
        """Возвращает детальную инфу по лимитам и рефералке."""
        user, _ = self.get_or_create_user(user_id)
        referral = user.setdefault("referral", {})
        usage = user.setdefault("usage", {})

        plan = user.get("plan", "free")
        plan_cfg = PLAN_LIMITS.get(plan, PLAN_LIMITS.get("free", {}))
        base_limit = int(plan_cfg.get("daily_base", 50))

        invited_users = referral.get("invited_users", [])
        invited_count = len(set(invited_users))

        ref_bonus = REF_BONUS_PER_USER * invited_count
        limit_today = base_limit + ref_bonus

        today = _today_str()
        used_today = int(usage.get(today, 0))
        total_requests = int(referral.get("total_requests", 0))

        return {
            "plan": plan,
            "plan_title": plan_cfg.get("title", plan),
            "used_today": used_today,
            "limit_today": limit_today,
            "base_limit": base_limit,
            "ref_bonus": ref_bonus,
            "invited_count": invited_count,
            "total_requests": total_requests,
        }

    def can_make_request(self, user_id: int) -> bool:
        limits = self.get_limits(user_id)
        return limits["used_today"] < limits["limit_today"]

    # ===== РЕФЕРАЛКА =====

    def _generate_ref_code(self, user_id: int) -> str:
        """Генерим стабильный реф-код на основе user_id."""
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        n = user_id if user_id > 0 else abs(user_id) + 1
        result = ""
        base = len(alphabet)
        while n > 0:
            n, r = divmod(n, base)
            result = alphabet[r] + result
        return f"BB{result}"  # префикс под бренд BlackBox

    def ensure_ref_code(self, user_id: int) -> str:
        user, _ = self.get_or_create_user(user_id)
        referral = user.setdefault("referral", {})
        code = referral.get("code")
        if not code:
            code = self._generate_ref_code(user_id)
            referral["code"] = code
            self._save()
        return code

    def _find_user_by_ref_code(self, code: str) -> int | None:
        users = self.data.get("users", {})
        for uid_str, udata in users.items():
            ref = udata.get("referral", {})
            if ref.get("code") == code:
                try:
                    return int(uid_str)
                except ValueError:
                    continue
        return None

    def attach_referral(self, invited_id: int, code: str) -> str:
        """Привязка пригласившего по коду.

        Возвращает:
          - "ok"
          - "not_found"
          - "already_has_referrer"
          - "self_referral"
        """
        invited, _ = self.get_or_create_user(invited_id)
        referral = invited.setdefault("referral", {})

        if referral.get("invited_by") is not None:
            return "already_has_referrer"

        owner_id = self._find_user_by_ref_code(code)
        if owner_id is None:
            return "not_found"
        if owner_id == invited_id:
            return "self_referral"

        referral["invited_by"] = owner_id

        owner, _ = self.get_or_create_user(owner_id)
        owner_ref = owner.setdefault("referral", {})
        invited_users = owner_ref.setdefault("invited_users", [])
        if invited_id not in invited_users:
            invited_users.append(invited_id)

        self._save()
        return "ok"

    def get_referral_stats(self, user_id: int) -> Dict[str, Any]:
        """Расширенная инфа для экрана «Рефералы»/«Профиль»."""
        user, _ = self.get_or_create_user(user_id)
        referral = user.setdefault("referral", {})
        limits = self.get_limits(user_id)

        return {
            "code": referral.get("code"),
            "invited_by": referral.get("invited_by"),
            "invited_count": limits["invited_count"],
            "plan": limits["plan"],
            "plan_title": limits["plan_title"],
            "used_today": limits["used_today"],
            "limit_today": limits["limit_today"],
            "base_limit": limits["base_limit"],
            "ref_bonus": limits["ref_bonus"],
            "total_requests": limits["total_requests"],
        }
