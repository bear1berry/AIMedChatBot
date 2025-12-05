import os
from pathlib import Path
from typing import List, Dict, Any

from dotenv import load_dotenv

# Базовая директория проекта
BASE_DIR = Path(__file__).resolve().parent.parent

# Загрузка .env
ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

# ===== Пути к файлам данных =====
DATA_DIR = BASE_DIR / "data"
USERS_FILE_PATH = str(DATA_DIR / "users.json")

# --- ОБЯЗАТЕЛЬНЫЕ ПЕРЕМЕННЫЕ ---

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables")

# Ключ и модель DeepSeek
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# username бота (для реф-ссылки)
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")

# Админы (без лимитов) – список id через запятую
ADMIN_USER_IDS: List[int] = []
raw_admins = os.getenv("ADMIN_USER_IDS", "")
for part in raw_admins.replace(" ", "").split(","):
    if part:
        try:
            ADMIN_USER_IDS.append(int(part))
        except ValueError:
            continue

# --- ТАРИФЫ / ЛИМИТЫ ---

PLAN_LIMITS: Dict[str, Dict[str, Any]] = {
    "free": {
        "code": "free",
        "title": "Базовый",
        "daily_base": int(os.getenv("FREE_DAILY_LIMIT", "30")),
        "description": "Без подписки. Подходит для знакомства с ботом.",
    },
    "premium": {
        "code": "premium",
        "title": "Premium",
        "daily_base": int(os.getenv("PREMIUM_DAILY_LIMIT", "250")),
        "description": "Расширенный лимит и приоритетные ответы.",
    },
}

# Сколько доп. запросов даёт один реферал
REF_BONUS_PER_USER = int(os.getenv("REF_BONUS_PER_USER", "5"))

# --- CryptoBot / USDT ---

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN", "")

# Тарифы Premium – только USDT через @CryptoBot
SUBSCRIPTION_TARIFFS: Dict[str, Dict[str, Any]] = {
    "premium_1m": {
        "key": "premium_1m",
        "plan": "premium",
        "amount": float(os.getenv("PREMIUM_PRICE_1M", "7.99")),
        "currency": "USDT",
        "duration_days": 30,
        "title": "Premium — 1 месяц",
    },
    "premium_3m": {
        "key": "premium_3m",
        "plan": "premium",
        "amount": float(os.getenv("PREMIUM_PRICE_3M", "26.99")),
        "currency": "USDT",
        "duration_days": 90,
        "title": "Premium — 3 месяца",
    },
    "premium_12m": {
        "key": "premium_12m",
        "plan": "premium",
        "amount": float(os.getenv("PREMIUM_PRICE_12M", "82.99")),
        "currency": "USDT",
        "duration_days": 365,
        "title": "Premium — 12 месяцев",
    },
}

# --- РЕЖИМЫ АССИСТЕНТА ---

ASSISTANT_MODES: Dict[str, Dict[str, str]] = {
    "universal": {
        "title": "Универсальный",
        "emoji": "🧠",
        "system_prompt": (
            "Ты универсальный ИИ-ассистент BlackBox GPT. "
            "Отвечаешь на любые вопросы — от жизни до кода. "
            "Пиши структурировано, по делу, без воды. "
            'Если можешь выдать список или шаги — делай это в формате "1., 2., 3.".'
        ),
    },
    "med": {
        "title": "Медицина",
        "emoji": "🩺",
        "system_prompt": (
            "Ты медицинский ассистент. Объясняй максимально аккуратно. "
            "Не ставь диагнозов и не назначай лечение. "
            "Всегда добавляй напоминание про необходимость очной консультации врача. "
            "Формат: кратко, понятно, без запугивания."
        ),
    },
    "mentor": {
        "title": "Наставник",
        "emoji": "🔥",
        "system_prompt": (
            "Ты личный наставник пользователя. "
            "Помогаешь в росте, дисциплине, привычках, мышлении. "
            "Говори прямо, но поддерживающе. "
            "Давай конкретные шаги, упражнения и вопросы для самоанализа."
        ),
    },
    "business": {
        "title": "Бизнес",
        "emoji": "💼",
        "system_prompt": (
            "Ты бизнес-аналитик и стратег. "
            "Помогаешь с идеями, проверкой гипотез, монетизацией, продуктом. "
            "Структурируй мысли пользователя, предлагай варианты действий, "
            "оценивай риски и возможный выхлоп."
        ),
    },
    "creative": {
        "title": "Креатив",
        "emoji": "🎨",
        "system_prompt": (
            "Ты креативный генератор идей. "
            "Помогаешь с текстами, дизайном, визуальными концепциями. "
            "Предлагай несколько вариантов, играй со стилями, "
            "но сохраняй вкус и чувство меры."
        ),
    },
    "voice_coach": {
        "title": "Голосовой коуч",
        "emoji": "🎧",
        "system_prompt": (
            "Ты голосовой коуч. Пользователь часто говорит сумбурно, голосовыми. "
            "Твоя задача — разобрать монолог, вытащить главное, "
            "структурировать мысли и вернуть в формате:\n\n"
            "1) Ключевые мысли\n"
            "2) Эмоции и состояние\n"
            "3) Задачи и решения\n"
            "4) Следующие шаги на ближайшие 24 часа\n\n"
            "Пиши лаконично, но по существу."
        ),
    },
}

DEFAULT_MODE_KEY = "universal"

# --- АУДИО (STT / TTS) ---

# AUDIO_PROVIDER = yandex | openai | deepseek (мы заложили интерфейс)
AUDIO_PROVIDER = os.getenv("AUDIO_PROVIDER", "yandex")
YANDEX_SPEECHKIT_API_KEY = os.getenv("YANDEX_SPEECHKIT_API_KEY", "")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "")

