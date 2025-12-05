"""
Глобальная конфигурация бота BlackBox GPT.

Здесь лежит всё, что нужно остальному коду:
- токены и API-ключи
- лимиты тарифов
- пути к файлам
- текст онбординга
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# === БАЗОВЫЕ ПУТИ ===

BASE_DIR: Path = Path(__file__).resolve().parent.parent
ENV_PATH: Path = BASE_DIR / ".env"
DATA_DIR: Path = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE_PATH: Path = DATA_DIR / "users.json"


# === ЗАГРУЗКА .env ===

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


# === ТОКЕНЫ И КЛЮЧИ ===

BOT_TOKEN: Optional[str] = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY: Optional[str] = os.getenv("DEEPSEEK_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment variables")

if not DEEPSEEK_API_KEY:
    raise RuntimeError("DEEPSEEK_API_KEY is not set in environment variables")

# DeepSeek / LLM
DEEPSEEK_API_URL: str = os.getenv(
    "DEEPSEEK_API_URL",
    "https://api.deepseek.com/v1/chat/completions",
)
DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


# === АДМИНЫ ===

# через запятую в .env: 123,456,789
_admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: List[int] = []
for part in _admin_ids_raw.split(","):
    part = part.strip()
    if part.isdigit():
        ADMIN_IDS.append(int(part))


# === ТАРИФЫ / ЛИМИТЫ ===

# два режима: BASIC и PREMIUM
# BASIC ограничен по количеству сообщений в день, PREMIUM — безлимит
PLAN_LIMITS: Dict[str, Dict[str, Any]] = {
    "basic": {
        "name": "Базовый",
        "daily_messages": 50,      # лимит в сутки
        "priority": 1,
    },
    "premium": {
        "name": "Premium",
        "daily_messages": None,    # None = без лимита
        "priority": 10,
    },
}

# сколько дополнительных сообщений даёт 1 приглашённый реферал
REF_BONUS_PER_USER: int = 20

# сколько последних сообщений диалога храним в истории для LLM
MAX_HISTORY_MESSAGES: int = 20


# === РЕЖИМЫ ПОМОЩНИКА ===

ASSISTANT_MODES: Dict[str, Dict[str, str]] = {
    "universal": {
        "title": "Универсальный 🤖",
        "system_prompt": (
            "Ты — BlackBox GPT, универсальный ИИ-ассистент. "
            "Отвечай чётко, структурированно и по делу. "
            "Если нужно — задавай уточняющие вопросы, но не перегружай пользователя."
        ),
    },
    "medicine": {
        "title": "Медицина 🩺",
        "system_prompt": (
            "Ты — помощник врача-эпидемиолога. "
            "Давай аккуратные, взвешенные ответы, опираясь на доказательную медицину. "
            "Всегда напоминай, что твои ответы не заменяют очную консультацию врача."
        ),
    },
    "mentor": {
        "title": "Наставник 🔥",
        "system_prompt": (
            "Ты — личный наставник пользователя. "
            "Помогаешь с дисциплиной, режимом, целями, даёшь мотивирующие, но честные ответы."
        ),
    },
    "business": {
        "title": "Бизнес 💼",
        "system_prompt": (
            "Ты — консультант по бизнесу, стратегиям и продуктам. "
            "Помогаешь структурировать идеи, считать экономику и находить точки роста."
        ),
    },
    "creative": {
        "title": "Креатив 🎨",
        "system_prompt": (
            "Ты — креативный партнёр. Придумываешь идеи, форматы, тексты, визуальные концепты."
        ),
    },
}

DEFAULT_MODE_KEY: str = "universal"


# === ОНБОРДИНГ / ТЕКСТЫ ===

BOT_NAME: str = "BlackBox GPT"
BOT_TAGLINE: str = "Universal AI Assistant"

ONBOARDING_TEXT: str = (
    f"🖤 <b>{BOT_NAME}</b> — {BOT_TAGLINE}.\n\n"
    "Минимум интерфейса. Максимум мозга.\n\n"
    "Просто напиши свой запрос — я разберусь.\n"
    "Нижний таскбар — для выбора режима, профиля, подписки и рефералов."
)


# === КРИПТО-ОПЛАТА (CryptoBot) ===

# токен Crypto Pay API (@CryptoBot), кладёшь в .env
CRYPTO_PAY_API_TOKEN: Optional[str] = os.getenv("CRYPTO_PAY_API_TOKEN")

# базовый URL API криптобота (его у нас как раз не хватало)
CRYPTO_PAY_API_URL: str = os.getenv(
    "CRYPTO_PAY_API_URL",
    "https://pay.crypt.bot/api",
)

# тарифы для кнопки "Подписка"
# code — внутренний код тарифа, на него завязана логика
SUBSCRIPTION_TARIFFS: Dict[str, Dict[str, Any]] = {
    "monthly": {
        "code": "monthly",
        "title": "1 месяц — $7.99",
        "amount": "7.99",
        "asset": "USDT",
        "period_days": 30,
    },
    "quarterly": {
        "code": "quarterly",
        "title": "3 месяца — $26.99",
        "amount": "26.99",
        "asset": "USDT",
        "period_days": 90,
    },
    "yearly": {
        "code": "yearly",
        "title": "12 месяцев — $82.99",
        "amount": "82.99",
        "asset": "USDT",
        "period_days": 365,
    },
}


# === ЯНДЕ
