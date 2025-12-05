import os
from pathlib import Path
from typing import Dict, Any, Set

from dotenv import load_dotenv

# ==============================
#   Базовые пути и .env
# ==============================

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

# Явно подгружаем .env, чтобы всё работало и через systemd, и локально
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE_PATH = DATA_DIR / "users.json"

# ==============================
#   Токены и API ключи
# ==============================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise RuntimeError("DEEPSEEK_API_KEY is not set in environment variables")

DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# ==============================
#   Настройки DeepSeek / режимов
# ==============================

DEFAULT_MODE_KEY = "universal"

ASSISTANT_MODES: Dict[str, Dict[str, Any]] = {
    "universal": {
        "title": "Универсальный",
        "emoji": "🧠",
        "system_prompt": (
            "Ты — универсальный ИИ-ассистент BlackBoxGPT. "
            "Отвечай кратко, по делу, дружелюбно, но без воды. "
            "Если вопрос не ясен — уточни. Если есть риски для здоровья или денег — предупреди."
        ),
    },
    "med": {
        "title": "Медицина",
        "emoji": "🩺",
        "system_prompt": (
            "Ты — профессиональный медицинский ассистент. "
            "Даёшь информацию максимально аккуратно, с оговорками: это не диагноз и не замена очной консультации. "
            "Структурируй ответы: симптомы, возможные причины, что можно сделать самостоятельно, когда срочно к врачу."
        ),
    },
    "mentor": {
        "title": "Наставник",
        "emoji": "🔥",
        "system_prompt": (
            "Ты — личный наставник пользователя Александр. "
            "Помогаешь в саморазвитии, дисциплине, режиме дня и психологии. "
            "Говори прямо, мотивируй, но без токсичности. Давай конкретные шаги и небольшие задания."
        ),
    },
    "business": {
        "title": "Бизнес",
        "emoji": "💼",
        "system_prompt": (
            "Ты — стратегический бизнес-ассистент. "
            "Помогаешь с идеями, продуктом, маркетингом, Telegram-каналами, монетизацией. "
            "Отвечай структурировано: анализ, идеи, пошаговый план."
        ),
    },
    "creative": {
        "title": "Креатив",
        "emoji": "🎨",
        "system_prompt": (
            "Ты — креативный ассистент. "
            "Помогаешь с идеями постов, визуалов, названий, описаний, промо. "
            "Предлагай несколько вариантов, будь смелее, но без кринжа."
        ),
    },
}

# ==============================
#   Тарифы и лимиты
# ==============================

PLAN_LIMITS: Dict[str, Dict[str, Any]] = {
    "free": {
        "title": "Базовый",
        "daily_base": int(os.getenv("PLAN_FREE_DAILY_LIMIT", "30")),
        "description": "До 30 ответов в день. Идеально, чтобы познакомиться с ботом.",
    },
    "premium": {
        "title": "Premium",
        "daily_base": int(os.getenv("PLAN_PREMIUM_DAILY_LIMIT", "300")),
        "description": "Расширенный лимит, приоритетные ответы и доступ ко всем режимам.",
    },
}

# ==============================
#   Реферальная программа
# ==============================

REF_BONUS_PER_USER = int(os.getenv("REF_BONUS_PER_USER", "20"))  # +20 запросов в день за реферала

# ==============================
#   Диалоговый контекст
# ==============================

MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "10"))

# ==============================
#   CryptoBot (Crypto Pay API)
# ==============================

CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")
CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")

# Цена в USDT за тарифы (можно поменять в .env, по умолчанию как ты просил)
SUBSCRIPTION_TARIFFS: Dict[str, Dict[str, Any]] = {
    "premium_1m": {
        "title": "Premium · 1 месяц",
        "amount": float(os.getenv("SUB_PREMIUM_1M_AMOUNT", "7.99")),
        "duration_days": int(os.getenv("SUB_PREMIUM_1M_DAYS", "30")),
    },
    "premium_3m": {
        "title": "Premium · 3 месяца",
        "amount": float(os.getenv("SUB_PREMIUM_3M_AMOUNT", "26.99")),
        "duration_days": int(os.getenv("SUB_PREMIUM_3M_DAYS", "90")),
    },
    "premium_12m": {
        "title": "Premium · 12 месяцев",
        "amount": float(os.getenv("SUB_PREMIUM_12M_AMOUNT", "82.99")),
        "duration_days": int(os.getenv("SUB_PREMIUM_12M_DAYS", "365")),
    },
}

# username бота для реф-ссылок, например: BlackBoxGPT_bot
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")

# ==============================
#   Админ (без ограничений)
# ==============================

_raw_admin_ids = os.getenv("ADMIN_USER_IDS", "").replace(";", ",")
ADMIN_USER_IDS: Set[int] = set()
for part in _raw_admin_ids.split(","):
    part = part.strip()
    if part.isdigit():
        ADMIN_USER_IDS.add(int(part))
