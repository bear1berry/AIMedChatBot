import json
import threading
from typing import Dict, Any, Optional
from pathlib import Path

from bot.config import (
    PLAN_LIMITS,
    REF_BONUS_PER_USER,
    MAX_HISTORY_MESSAGES,
    ADMIN_USER_IDS,
)

# Локально считаем путь к файлу пользователей,
# чтобы не зависеть от USERS_FILE_PATH в config.py
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_FILE_PATH = str(DATA_DIR / "users.json")

def _today_str() -> str:
    # Можно заменить на UTC по желанию
    return date.today().isoformat()


def _now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


class Storage:
    """
    Простое файловое хранилище (JSON) под пользователей, лимиты, рефералку и подписки.
    """

    def __init__(self, path: Path | str = USERS_FILE_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: Dict[str, Dict[str, Any]] = {}
        self._load()

    # =========================
    #   Внутренние методы
    # =========================

    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                # если файл битый — не роняем бота
                self.data = {}
        else:
            self.data = {}
            self._save()

    def _save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            tmp_path.replace(self.path)

    def _get_or_create_user_internal(self, user_id: int) -> Dict[str, Any]:
        uid = str(user_id)
        user = self.data.get(uid)
        if user is None:
            user = {
                "created_at": _now_iso(),
                "mode_key": DEFAULT_MODE_KEY,
                "plan": "free",
                "plan_expires_at": None,
                "usage": {
                    "date": _today_str(),
                    "used_today": 0,
                    "total_requests": 0,
                },
                "history": [],
                "dossier": {},
                "referral": {},
                "payments": {"invoices": []},
                "onboarding_seen": False,
            }
            self.data[uid] = user
            self._save()
        return user

    # =========================
    #   Пользователь / онбординг
    # =========================

    def get_or_create_user(self, user_id: int) -> Tuple[Dict[str, Any], bool]:
        """
        Возвращает (user_dict, is_new).
        """
        uid = str(user_id)
        is_new = uid not in self.data
        user = self._get_or_create_user_internal(user_id)
        return user, is_new

    def mark_onboarding_seen(self, user_id: int) -> None:
        user = self._get_or_create_user_internal(user_id)
        if not user.get("onboarding_seen"):
            user["onboarding_seen"] = True
            self._save()

    # =========================
    #   Режимы
    # =========================

    def get_mode(self, user_id: int) -> str:
        user = self._get_or_create_user_internal(user_id)
        return user.get("mode_key", DEFAULT_MODE_KEY)

    def update_mode(self, user_id: int, mode_key: str) -> None:
        user = self._get_or_create_user_internal(user_id)
        user["mode_key"] = mode_key
        self._save()

    # =========================
    #   История диалога
    # =========================

    def append_history(self, user_id: int, role: str, content: str) -> None:
        user = self._get_or_create_user_internal(user_id)
        history: List[Dict[str, str]] = user.setdefault("history", [])
        history.append({"role": role, "content": content})
        # ограничиваем длину истории
        if len(history) > MAX_HISTORY_MESSAGES * 2:
            # *2 потому что считаем user+assistant
            history[:] = history[-MAX_HISTORY_MESSAGES * 2 :]
        self._save()

    def get_history(self, user_id: int) -> List[Dict[str, str]]:
        user = self._get_or_create_user_internal(user_id)
        return list(user.get("history", []))

    def clear_history(self, user_id: int) -> None:
        user = self._get_or_create_user_internal(user_id)
        user["history"] = []
        self._save()

    # =========================
    #   Досье (простейшая реализация)
    # =========================

    def update_dossier_on_message(self, user_id: int, mode_key: str, user_prompt: str) -> None:
        """
        Для простоты: храним последние N пользовательских сообщений в досье.
        В будущем можно заменить на автосуммаризацию.
        """
        user = self._get_or_create_user_internal(user_id)
        dossier = user.setdefault("dossier", {})
        msgs: List[str] = dossier.setdefault("messages", [])
        msgs.append(user_prompt)
        if len(msgs) > 100:
            msgs[:] = msgs[-100:]
        dossier["updated_at"] = _now_iso()
        self._save()

    def get_dossier_preview(self, user_id: int) -> str:
        user = self._get_or_create_user_internal(user_id)
        dossier = user.get("dossier", {})
        msgs: List[str] = dossier.get("messages", [])
        if not msgs:
            return "Пока досье пустое. Спрашивай, работай — и я буду постепенно лучше понимать твой контекст."
        # показываем несколько последних фраз
        last_msgs = msgs[-5:]
        joined = "\n• ".join(last_msgs)
        return f"Небольшой срез по твоим последним запросам:\n\n• {joined}"

    # =========================
    #   Лимиты / подписки / админ
    # =========================

    def _refresh_usage_and_plan(self, user_id: int) -> Dict[str, Any]:
        user = self._get_or_create_user_internal(user_id)
        usage = user.setdefault(
            "usage",
            {"date": _today_str(), "used_today": 0, "total_requests": 0},
        )
        today = _today_str()
        if usage.get("date") != today:
            usage["date"] = today
            usage["used_today"] = 0

        # проверяем истечение подписки
        plan = user.get("plan", "free")
        expires_at = user.get("plan_expires_at")
        if plan != "free" and expires_at:
            try:
                expires_dt = datetime.fromisoformat(expires_at.replace("Z", ""))
                if datetime.utcnow() > expires_dt:
                    user["plan"] = "free"
                    user["plan_expires_at"] = None
                    plan = "free"
            except Exception:
                # если вдруг битая дата — обнулим
                user["plan"] = "free"
                user["plan_expires_at"] = None
                plan = "free"

        self._save()
        return user

    def get_limits(self, user_id: int) -> Dict[str, Any]:
        user = self._refresh_usage_and_plan(user_id)
        usage = user["usage"]

        is_admin = user_id in ADMIN_USER_IDS

        # базовый план и лимиты
        plan = user.get("plan", "free")
        plan_cfg = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
        base_limit = int(plan_cfg["daily_base"])

        # реферальный бонус
        invited_count = self._count_invited_users(user_id)
        ref_bonus = invited_count * REF_BONUS_PER_USER

        if is_admin:
            limit_today = 10**9  # практически бесконечный лимит
        else:
            limit_today = base_limit + ref_bonus

        return {
            "plan": plan,
            "plan_title": plan_cfg["title"],
            "used_today": int(usage.get("used_today", 0)),
            "limit_today": limit_today,
            "base_limit": base_limit,
            "ref_bonus": ref_bonus,
            "invited_count": invited_count,
            "total_requests": int(usage.get("total_requests", 0)),
            "is_admin": is_admin,
        }

    def can_make_request(self, user_id: int) -> bool:
        limits = self.get_limits(user_id)
        if limits["is_admin"]:
            return True
        return limits["used_today"] < limits["limit_today"]

    def increment_usage(self, user_id: int) -> None:
        user = self._refresh_usage_and_plan(user_id)
        usage = user["usage"]
        usage["used_today"] = int(usage.get("used_today", 0)) + 1
        usage["total_requests"] = int(usage.get("total_requests", 0)) + 1
        self._save()

    def set_plan(self, user_id: int, plan: str, duration_days: int | None = None) -> None:
        user = self._get_or_create_user_internal(user_id)
        user["plan"] = plan
        if duration_days and duration_days > 0:
            expires_dt = datetime.utcnow() + timedelta(days=duration_days)
            user["plan_expires_at"] = expires_dt.isoformat(timespec="seconds") + "Z"
        else:
            user["plan_expires_at"] = None
        self._save()

    def get_plan_info(self, user_id: int) -> Dict[str, Any]:
        user = self._refresh_usage_and_plan(user_id)
        plan = user.get("plan", "free")
        plan_cfg = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
        return {
            "plan": plan,
            "plan_title": plan_cfg["title"],
            "plan_expires_at": user.get("plan_expires_at"),
        }

    # =========================
    #   Рефералка
    # =========================

    def _generate_ref_code(self, user_id: int) -> str:
        # Простая генерация кода: user_id + проверочная буква
        base = str(user_id)
        checksum = str(sum(ord(ch) for ch in base) % 10)
        return base + checksum

    def ensure_referral_code(self, user_id: int) -> str:
        user = self._get_or_create_user_internal(user_id)
        ref = user.setdefault("referral", {})
        code = ref.get("code")
        if not code:
            code = self._generate_ref_code(user_id)
            ref["code"] = code
            self._save()
        return code

    def _count_invited_users(self, user_id: int) -> int:
        """
        Считаем количество пользователей, у которых invited_by == code пользователя.
        """
        code = self.ensure_referral_code(user_id)
        cnt = 0
        for u in self.data.values():
            ref = u.get("referral") or {}
            if ref.get("invited_by") == code:
                cnt += 1
        return cnt

    def register_referral(self, new_user_id: int, ref_code: str) -> None:
        """
        Вызывается при /start с параметром.
        """
        # Находим, существует ли такой код
        owner_user_id: int | None = None
        for uid_str, u in self.data.items():
            ref = u.get("referral") or {}
            if ref.get("code") == ref_code:
                owner_user_id = int(uid_str)
                break

        if owner_user_id is None:
            return

        # Обновляем нового пользователя
        new_user = self._get_or_create_user_internal(new_user_id)
        ref = new_user.setdefault("referral", {})
        # чтобы не перезаписывать, если уже кто-то привязал
        if not ref.get("invited_by"):
            ref["invited_by"] = ref_code
            self._save()

    def get_referral_info(self, user_id: int) -> Dict[str, Any]:
        user = self._get_or_create_user_internal(user_id)
        code = self.ensure_referral_code(user_id)
        invited_count = self._count_invited_users(user_id)
        limits = self.get_limits(user_id)

        ref = user.get("referral") or {}
        return {
            "code": code,
            "invited_by": ref.get("invited_by"),
            "invited_count": invited_count,
            "plan": limits["plan"],
            "plan_title": limits["plan_title"],
            "used_today": limits["used_today"],
            "limit_today": limits["limit_today"],
            "base_limit": limits["base_limit"],
            "ref_bonus": limits["ref_bonus"],
            "total_requests": limits["total_requests"],
        }

    # =========================
    #   Платежи / CryptoBot
    # =========================

    def register_invoice(
        self,
        user_id: int,
        invoice_id: int,
        plan: str,
        duration_days: int,
    ) -> None:
        user = self._get_or_create_user_internal(user_id)
        payments = user.setdefault("payments", {})
        invoices: List[Dict[str, Any]] = payments.setdefault("invoices", [])

        # если вдруг уже есть такой invoice_id — не дублируем
        for inv in invoices:
            if inv.get("invoice_id") == int(invoice_id):
                return

        invoices.append(
            {
                "invoice_id": int(invoice_id),
                "plan": plan,
                "duration_days": duration_days,
                "status": "active",
                "created_at": _now_iso(),
            }
        )
        self._save()

    def update_invoice_status(
        self,
        user_id: int,
        invoice_id: int,
        status: str,
    ) -> None:
        user = self._get_or_create_user_internal(user_id)
        payments = user.setdefault("payments", {})
        invoices: List[Dict[str, Any]] = payments.setdefault("invoices", [])
        for inv in invoices:
            if inv.get("invoice_id") == int(invoice_id):
                inv["status"] = status
                inv["updated_at"] = _now_iso()
                break
        self._save()

    def iter_invoices(self, statuses: Iterable[str] = ("active",)) -> Iterable[tuple[int, Dict[str, Any]]]:
        """
        Итерируемся по (user_id, invoice_dict) для нужных статусов.
        """
        wanted = set(statuses)
        for uid_str, user in self.data.items():
            payments = user.get("payments") or {}
            for inv in payments.get("invoices", []):
                if inv.get("status") in wanted:
                    yield int(uid_str), inv

