import os
from pathlib import Path
from typing import Dict, Any, List

from dotenv import load_dotenv

# =========================
#  Базовые пути и .env
# =========================

BASE_DIR: Path = Path(__file__).resolve().parent.parent
ENV_PATH: Path = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

# =========================
#  Ключи и токены
# =========================

BOT_TOKEN: str | None = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY: str | None = os.getenv("DEEPSEEK_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables")

if not DEEPSEEK_API_KEY:
    raise RuntimeError("DEEPSEEK_API_KEY is not set in environment variables")

# DeepSeek
DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Админы (через запятую, например: ADMIN_IDS=123,456)
ADMIN_IDS: List[int] = []
_raw_admins = os.getenv("ADMIN_IDS", "")
if _raw_admins.strip():
    for part in _raw_admins.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_IDS.append(int(part))

# =========================
#  Хранилище пользователей
# =========================

DATA_DIR: Path = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

USERS_FILE_PATH: Path = DATA_DIR / "users.json"

# =========================
#  Режимы ассистента
# =========================

ASSISTANT_MODES: Dict[str, Dict[str, Any]] = {
    "universal": {
        "key": "universal",
        "emoji": "🧠",
        "title": "Универсальный",
        "description": "Мозг на все случаи жизни: от идей и текстов до кода и быта.",
        "system_prompt": (
            "Ты — BlackBox GPT, универсальный ИИ-ассистент. "
            "Отвечай структурировано, по делу, без воды. "
            "Сначала короткий вывод, затем — подробности по пунктам. "
            "Уважай личные границы пользователя, не дави, не морализируй."
        ),
    },
    "medicine": {
        "key": "medicine",
        "emoji": "🩺",
        "title": "Медицина",
        "description": "Медицинский режим: эпидемиология, организация здравоохранения, доказательная база.",
        "system_prompt": (
            "Ты — ИИ-помощник врача-эпидемиолога. "
            "Оперируй только актуальными клиническими рекомендациями и доказательной медициной. "
            "Не ставь диагнозы и не назначай лечение, а давай информацию, ориентируясь на врача как на основного пользователя. "
            "Всегда добавляй дисклеймер, что это не замена очной консультации."
        ),
    },
    "mentor": {
        "key": "mentor",
        "emoji": "🔥",
        "title": "Наставник",
        "description": "Личный наставник: фокус, дисциплина, рост без соплей.",
        "system_prompt": (
            "Ты — личный наставник пользователя. "
            "Тон — поддерживающий, но прямой. Никакой жалости, только конструктив, конкретика и ответственность. "
            "Помогай выстроить режим, дисциплину, карьеру и личный рост. "
            "Каждый ответ заканчивай 1–3 чёткими действиями 'что сделать сегодня'."
        ),
    },
    "business": {
        "key": "business",
        "emoji": "💼",
        "title": "Бизнес",
        "description": "Стратегия, продукты, деньги, Telegram-проекты.",
        "system_prompt": (
            "Ты — стратегический бизнес-ассистент с фокусом на цифровые продукты и Telegram-экосистему. "
            "Думай как продуктолог и предприниматель: юнит-экономика, гипотезы, воронки, автоворонки, монетизация. "
            "Отвечай по структуре: 1) Анализ, 2) Идеи, 3) Конкретный план действий."
        ),
    },
    "creative": {
        "key": "creative",
        "emoji": "🎨",
        "title": "Креатив",
        "description": "Идеи, тексты, стили, визуальные промпты.",
        "system_prompt": (
            "Ты — креативный директор и сценарист. "
            "Генерируй сильные идеи, образы, стили. "
            "Уделяй внимание ритму текста, атмосфере, минимализму и премиальности. "
            "Избегай штампов и клише, предлагай нестандартные углы."
        ),
    },
}

DEFAULT_MODE_KEY: str = "universal"

# =========================
#  Лимиты, планы и рефералка
# =========================

PLAN_LIMITS: Dict[str, Dict[str, Any]] = {
    # БАЗОВЫЙ (Free/Basic)
    "basic": {
        "code": "basic",
        "title": "Базовый",
        "daily_limit": int(os.getenv("PLAN_BASIC_DAILY_LIMIT", "50")),
        "priority": 0,
    },
    "free": {  # алиас
        "code": "basic",
        "title": "Базовый",
        "daily_limit": int(os.getenv("PLAN_BASIC_DAILY_LIMIT", "50")),
        "priority": 0,
    },
    # PREMIUM
    "premium": {
        "code": "premium",
        "title": "Premium",
        "daily_limit": int(os.getenv("PLAN_PREMIUM_DAILY_LIMIT", "500")),
        "priority": 1,
    },
    "pro": {  # алиас к премиуму
        "code": "premium",
        "title": "Premium",
        "daily_limit": int(os.getenv("PLAN_PREMIUM_DAILY_LIMIT", "500")),
        "priority": 1,
    },
    "vip": {  # ещё один алиас, чтобы не ломать старые данные
        "code": "premium",
        "title": "Premium",
        "daily_limit": int(os.getenv("PLAN_PREMIUM_DAILY_LIMIT", "500")),
        "priority": 1,
    },
}

# Бонус к дневному лимиту за каждого приглашённого пользователя
REF_BONUS_PER_USER: int = int(os.getenv("REF_BONUS_PER_USER", "20"))

# Сколько последних сообщений храним в контексте диалога
MAX_HISTORY_MESSAGES: int = int(os.getenv("MAX_HISTORY_MESSAGES", "10"))

# =========================
#  CryptoBot — оплата в USDT
# =========================

CRYPTO_PAY_API_URL: str = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api/")
CRYPTO_PAY_API_TOKEN: str | None = os.getenv("CRYPTO_PAY_API_TOKEN")

# Тарифы подписки — используются и в интерфейсе, и в payments.py
SUBSCRIPTION_TARIFFS: Dict[str, Dict[str, Any]] = {
    "premium_1m": {
        "code": "premium_1m",
        "plan": "premium",
        "title": "Premium • 1 месяц",
        "amount": "7.99",      # USD
        "asset": "USDT",
        "period_days": 30,
        "description": "Подписка Premium на 1 месяц",
    },
    "premium_3m": {
        "code": "premium_3m",
        "plan": "premium",
        "title": "Premium • 3 месяца",
        "amount": "26.99",
        "asset": "USDT",
        "period_days": 90,
        "description": "Подписка Premium на 3 месяца",
    },
    "premium_12m": {
        "code": "premium_12m",
        "plan": "premium",
        "title": "Premium • 12 месяцев",
        "amount": "82.99",
        "asset": "USDT",
        "period_days": 365,
        "description": "Подписка Premium на 12 месяцев",
    },
}

# =========================
#  Яндекс SpeechKit (для future STT/TTS)
# =========================

YANDEX_FOLDER_ID: str | None = os.getenv("YANDEX_FOLDER_ID")
YANDEX_SPEECHKIT_API_KEY: str | None = os.getenv("YANDEX_SPEECHKIT_API_KEY")

# =========================
#  Тексты
# =========================

BOT_NAME: str = os.getenv("BOT_NAME", "BlackBox GPT")
BOT_TAGLINE: str = os.getenv("BOT_TAGLINE", "Universal AI Assistant")

ONBOARDING_TEXT: str = (
    f"Привет! Я {BOT_NAME} — {BOT_TAGLINE}.\n\n"
    "На экране только текст и нижний таскбар.\n"
    "Выбери режим, задай вопрос — и я сделаю остальное.\n\n"
    "Режимы переключаются снизу, подписка и рефералы — тоже там.\n"
    "Пиши как есть, без формальностей."
)


def print_debug_config() -> None:
    """Выводит важные флаги при старте бота."""
    print(f"[CONFIG] BASE_DIR={BASE_DIR}")
    print(f"[CONFIG] ENV_PATH={ENV_PATH} exists={ENV_PATH.exists()}")
    print(f"[CONFIG] BOT_TOKEN loaded? {'YES' if BOT_TOKEN else 'NO'}")
    print(f"[CONFIG] DEEPSEEK_API_KEY loaded? {'YES' if DEEPSEEK_API_KEY else 'NO'}")
    print(f"[CONFIG] CRYPTO_PAY_API_TOKEN loaded? {'YES' if CRYPTO_PAY_API_TOKEN else 'NO'}")


print_debug_config()
