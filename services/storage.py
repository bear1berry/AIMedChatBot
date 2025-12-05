import json
import os
import threading
from datetime import datetime, date

from bot.config import (
    DEFAULT_MODE_KEY,
    REF_BONUS_PER_USER,
    MAX_HISTORY_MESSAGES,
    PLAN_LIMITS,
)


def _today_str():
    """Сегодняшняя дата в формате YYYY-MM-DD."""
    return date.today().isoformat()


class Storage:
    """
    Простое файловое хранилище в JSON.

    Хранит:
      - досье пользователя (mode_key, messages_count, last_activity)
      - тариф (plan)
      - usage по дням (для суточных лимитов)
      - реферальную систему (code, invited_by, invited_users, total_requests)
      - диалоговую историю (history) для контекста LLM
    """

    def __init__(self, path="data/users.json"):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.data = {"users": {}}
        self._load()

    # ===== ВНУТРЕННЕЕ =====

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                # Если файл битый — стартуем с пустой структурой
                self.data = {"users": {}}
        else:
            self._save()

    def _save(self):
        with self._lock:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    def _user_key(self, user_id):
        return str(user_id)

    def _get_or_create_user_internal(self, user_id):
        """
        Внутренний метод: гарантирует наличие пользователя в self.data
        и всех нужных полей.
        """
        uid = self._user_key(user_id)
        users = self.data.setdefault("users", {})

        if uid not in users:
            # Новый пользователь
            users[uid] = {
                "mode_key": DEFAULT_MODE_KEY,
                "plan": "free",
                "dossier": {
                    "messages_count": 0,
                    "last_prompt_preview": "",
                    "last_activity": None,
                },
                "referral": {
                    "code": None,
                    "invited_by": None,
                    "invited_users": [],
                    "total_requests": 0,
                },
                "usage": {},    # { "YYYY-MM-DD": used_today }
                "history": [],  # диалоговая история
            }
        else:
            user = users[uid]
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
            user.setdefault("usage", {})
            user.setdefault("history", [])

        return users[uid]

    # ===== ПОЛЬЗОВАТЕЛЬ / ДОСЬЕ =====

    def get_or_create_user(self, user_id):
        """
        Возвращает словарь пользователя. Если его не было — создаёт.
        (main.py ожидает именно dict.)
        """
        user = self._get_or_create_user_internal(user_id)
        self._save()
        return user

    def get_dossier(self, user_id):
        user = self._get_or_create_user_internal(user_id)
        return user.get("dossier", {})

    def update_dossier_on_message(self, user_id, mode_key, user_prompt):
        """
        Обновляем «досье» при новом сообщении пользователя.
        """
        user = self._get_or_create_user_internal(user_id)
        dossier = user.setdefault("dossier", {})
        dossier["messages_count"] = int(dossier.get("messages_count", 0)) + 1
        dossier["last_prompt_preview"] = user_prompt[:120]
        dossier["last_activity"] = datetime.utcnow().isoformat()
        user["mode_key"] = mode_key
        self._save()

    # ===== ИСТОРИЯ ДИАЛОГА =====

    def get_history(self, user_id):
        user = self._get_or_create_user_internal(user_id)
        history = user.setdefault("history", [])
        if not isinstance(history, list):
            history = []
            user["history"] = history
            self._save()
        return history

    def append_history(self, user_id, role, content):
        """
        Добавляет сообщение в историю и ограничивает её длину MAX_HISTORY_MESSAGES.
        """
        user = self._get_or_create_user_internal(user_id)
        history = user.setdefault("history", [])
        if not isinstance(history, list):
            history = []
            user["history"] = history

        history.append({"role": role, "content": content})
        if len(history) > MAX_HISTORY_MESSAGES:
            history[:] = history[-MAX_HISTORY_MESSAGES:]
        self._save()

    def reset_history(self, user_id):
        user = self._get_or_create_user_internal(user_id)
        user["history"] = []
        self._save()

    # ===== ТАРИФЫ =====

    def get_plan(self, user_id):
        user = self._get_or_create_user_internal(user_id)
        return user.get("plan", "free")

    def set_plan(self, user_id, plan):
        if plan not in PLAN_LIMITS:
            return
        user = self._get_or_create_user_internal(user_id)
        user["plan"] = plan
        self._save()

    # ===== ЛИМИТЫ / ЗАПРОСЫ =====

    def register_request(self, user_id):
        """
        Увеличивает счётчики запросов: суточный и суммарный.
        """
        user = self._get_or_create_user_internal(user_id)
        referral = user.setdefault("referral", {})
        usage = user.setdefault("usage", {})

        referral["total_requests"] = int(referral.get("total_requests", 0)) + 1

        today = _today_str()
        usage[today] = int(usage.get(today, 0)) + 1

        self._save()

    def get_limits(self, user_id):
        """
        Возвращает подробную информацию по лимитам и рефералке:
        {
          "plan": "free" / "pro" / "vip",
          "plan_title": str,
          "used_today": int,
          "limit_today": int,
          "base_limit": int,
          "ref_bonus": int,
          "invited_count": int,
          "total_requests": int,
        }
        """
        user = self._get_or_create_user_internal(user_id)
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

    def can_make_request(self, user_id):
        limits = self.get_limits(user_id)
        return limits["used_today"] < limits["limit_today"]

    # ===== РЕФЕРАЛКА =====

    def _generate_ref_code(self, user_id):
        """
        Генерируем устойчивый реф-код на основе user_id.
        """
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        n = user_id if user_id > 0 else abs(user_id) + 1
        result = ""
        base = len(alphabet)
        while n > 0:
            n, r = divmod(n, base)
            result = alphabet[r] + result
        return f"BB{result}"

    def ensure_ref_code(self, user_id):
        """
        Убеждаемся, что у пользователя есть реф-код. Если нет — генерируем.
        Возвращает сам код.
        """
        user = self._get_or_create_user_internal(user_id)
        referral = user.setdefault("referral", {})
        code = referral.get("code")
        if not code:
            code = self._generate_ref_code(user_id)
            referral["code"] = code
            self._save()
        return code

    def _find_user_by_ref_code(self, code):
        """
        Ищем владельца данного реф-кода, возвращаем user_id или None.
        """
        for uid_str, udata in self.data.get("users", {}).items():
            ref = udata.get("referral", {})
            if ref.get("code") == code:
                try:
                    return int(uid_str)
                except ValueError:
                    continue
        return None

    def attach_referral(self, invited_id, code):
        """
        Привязка пригласившего по коду.
        Возвращает статус:
          - "ok"
          - "not_found"
          - "already_has_referrer"
          - "self_referral"
        """
        invited = self._get_or_create_user_internal(invited_id)
        referral = invited.setdefault("referral", {})

        # Уже есть приглашавший — не трогаем
        if referral.get("invited_by") is not None:
            return "already_has_referrer"

        owner_id = self._find_user_by_ref_code(code)
        if owner_id is None:
            return "not_found"
        if owner_id == invited_id:
            return "self_referral"

        referral["invited_by"] = owner_id

        owner = self._get_or_create_user_internal(owner_id)
        owner_ref = owner.setdefault("referral", {})
        invited_users = owner_ref.setdefault("invited_users", [])
        if invited_id not in invited_users:
            invited_users.append(invited_id)

        self._save()
        return "ok"

    def get_referral_stats(self, user_id):
        """
        Расширенная инфа для экрана «Рефералы» / профиля.
        """
        user = self._get_or_create_user_internal(user_id)
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
