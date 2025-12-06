from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List

from dotenv import load_dotenv

# Базовые пути
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"{name} is not set in environment variables")
    return value or ""


BOT_TOKEN = _get_env("BOT_TOKEN", required=True)
DEEPSEEK_API_KEY = _get_env("DEEPSEEK_API_KEY", required=True)

# DeepSeek API
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# CryptoBot (USDT only)
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api/")
CRYPTO_PAY_API_TOKEN = _get_env("CRYPTO_PAY_API_TOKEN", required=False)

# Storage
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
USERS_FILE_PATH = DATA_DIR / "users.json"

# Referrals
REF_BASE_URL = os.getenv("REF_BASE_URL", "https://t.me/BlackBoxGPT_bot")

# Admins
ADMIN_IDS: List[int] = []
_admin_ids_raw = os.getenv("ADMIN_IDS", "")
for part in _admin_ids_raw.replace(";", ",").split(","):
    part = part.strip()
    if part.isdigit():
        ADMIN_IDS.append(int(part))

LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0") or 0)

# Limits (по запросам, а не по токенам)
MAX_INPUT_TOKENS = int(os.getenv("MAX_INPUT_TOKENS", "4000"))

FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "20"))
FREE_MONTHLY_LIMIT = int(os.getenv("FREE_MONTHLY_LIMIT", "400"))

PREMIUM_DAILY_LIMIT = int(os.getenv("PREMIUM_DAILY_LIMIT", "200"))
PREMIUM_MONTHLY_LIMIT = int(os.getenv("PREMIUM_MONTHLY_LIMIT", "8000"))

# Subscription tariffs (USDT)
SUBSCRIPTION_TARIFFS: Dict[str, Dict[str, Any]] = {
    "month_1": {
        "code": "premium_1m",
        "title": "Premium · 1 месяц",
        "months": 1,
        "price_usdt": "7.99",
    },
    "month_3": {
        "code": "premium_3m",
        "title": "Premium · 3 месяца",
        "months": 3,
        "price_usdt": "26.99",
    },
    "month_12": {
        "code": "premium_12m",
        "title": "Premium · 12 месяцев",
        "months": 12,
        "price_usdt": "82.99",
    },
}

# Assistant modes
@dataclass(frozen=True)
class AssistantMode:
    key: str
    title: str
    system_prompt: str


ASSISTANT_MODES: Dict[str, Dict[str, Any]] = {
    "universal": {
        "key": "universal",
        "title": "Универсальный ассистент",
        "system_prompt": (
            "Ты — BlackBox GPT, универсальный ИИ-ассистент премиум-класса. "
            "Отвечай структурированно, по делу, без воды. "
            "Пиши чистый, аккуратный текст с заголовками, списками и акцентами, "
            "если это уместно. Адаптируй стиль под пользователя: язык, тон, "
            "уровень формальности. Без лишних эмодзи, только где они усиливают смысл."
        ),
    },
    "medicine": {
        "key": "medicine",
        "title": "Медицинский режим",
        "system_prompt": (
            "Ты — медицинский ИИ-ассистент. Объясняй понятно, без паники, "
            "с опорой на доказательную медицину. Не ставь диагнозы и не назначай лечение — "
            "давай информацию, возможные направления и обязательно рекомендуй очную "
            "консультацию врача при серьёзных симптомах."
        ),
    },
    "coach": {
        "key": "coach",
        "title": "Наставник и коуч",
        "system_prompt": (
            "Ты — наставник, коуч и партнёр по росту. Помогаешь выстраивать систему, "
            "даёшь структурные шаги, задаёшь сильные вопросы. Мотивация — без соплей, "
            "уважительно, но твёрдо."
        ),
    },
    "business": {
        "key": "business",
        "title": "Бизнес и стратегия",
        "system_prompt": (
            "Ты — стратегический бизнес-ассистент. Анализируешь идеи, рынки, процессы, "
            "помогаешь считать деньги и риски. Отвечай чётко, структурно, с прицелом на практику."
        ),
    },
    "creative": {
        "key": "creative",
        "title": "Креатив и идеи",
        "system_prompt": (
            "Ты — креативный партнёр. Генерируешь идеи, концепции, тексты, образы. "
            "Помни про минимализм, вкус и уместность. Лучше меньше, но сильнее."
        ),
    },
}

DEFAULT_MODE_KEY = "universal"
