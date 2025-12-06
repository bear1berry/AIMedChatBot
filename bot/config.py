from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


# --- Base paths ---

BASE_DIR: Path = Path(__file__).resolve().parent.parent
ENV_PATH: Path = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
    print(f"[CONFIG] BASE_DIR={BASE_DIR}")
    print(f"[CONFIG] ENV_PATH={ENV_PATH} exists=True")
else:
    print(f"[CONFIG] BASE_DIR={BASE_DIR}")
    print(f"[CONFIG] ENV_PATH={ENV_PATH} exists=False")

# --- Tokens & API keys ---

BOT_TOKEN: str | None = os.getenv("BOT_TOKEN") or "TEST_BOT_TOKEN"
DEEPSEEK_API_KEY: str | None = os.getenv("DEEPSEEK_API_KEY") or "TEST_DEEPSEEK_KEY"
␊
if not os.getenv("BOT_TOKEN"):
    print("[CONFIG] BOT_TOKEN is not set; using a placeholder value for local runs.")
if not os.getenv("DEEPSEEK_API_KEY"):
    print("[CONFIG] DEEPSEEK_API_KEY is not set; using a placeholder value for local runs.")
␊
print(f"[CONFIG] BOT_TOKEN loaded? {'YES' if BOT_TOKEN else 'NO'}")␊
print(f"[CONFIG] DEEPSEEK_API_KEY loaded? {'YES' if DEEPSEEK_API_KEY else 'NO'}")␊

# Optional: DeepSeek base URL & model (OpenAI-совместимый API)
DEEPSEEK_API_BASE_URL: str = os.getenv("DEEPSEEK_API_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# --- CryptoBot (Crypto Pay API) ---

CRYPTO_PAY_API_TOKEN: str = os.getenv("CRYPTO_PAY_API_TOKEN", "").strip()
CRYPTO_PAY_API_URL: str = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")

# --- Owner / admin ---

OWNER_ID_ENV = os.getenv("OWNER_ID", "").strip()
OWNER_ID = int(OWNER_ID_ENV) if OWNER_ID_ENV.isdigit() else None
ADMIN_IDS = [OWNER_ID] if OWNER_ID is not None else []
LOG_CHAT_ID_ENV = os.getenv("LOG_CHAT_ID", "").strip()
LOG_CHAT_ID = int(LOG_CHAT_ID_ENV) if LOG_CHAT_ID_ENV.isdigit() else None

# --- Misc ---
REF_BASE_URL = os.getenv("REF_BASE_URL", "https://t.me/yourbot")
MAX_INPUT_TOKENS = int(os.getenv("MAX_INPUT_TOKENS", "2000"))

# --- Планы и лимиты ---

PLAN_BASIC = "basic"
PLAN_PREMIUM = "premium"

# Сколько сообщений в день у базового плана
DEFAULT_DAILY_LIMIT: int = 30

# Сколько дополнительных сообщений в день даёт один реферал
REF_BONUS_PER_USER: int = 10

# Тарифы подписки (цены в USDT через CryptoBot)
SUBSCRIPTION_TARIFFS = {␊
    "month_1": {
        "code": "month_1",
        "title": "1 месяц Premium",␊
        "days": 30,␊
        "price_usdt": 7.99,␊
        "asset": "USDT",␊
    },␊
    "month_3": {
        "code": "month_3",
        "title": "3 месяца Premium",␊
        "days": 90,␊
        "price_usdt": 26.99,␊
        "asset": "USDT",␊
    },␊
    "month_12": {
        "code": "month_12",
        "title": "12 месяцев Premium",␊
        "days": 365,␊
        "price_usdt": 82.99,␊
        "asset": "USDT",␊
    },␊
}␊

# --- Файлы хранилища ---

DATA_DIR: Path = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
USERS_FILE_PATH: Path = DATA_DIR / "users.json"

# --- Режимы ассистента ---

DEFAULT_MODE = "universal"

ASSISTANT_MODES = {
    "universal": {
        "code": "universal",
        "emoji": "🧠",
        "title": "Универсальный",
        "button": "🧠 Универсальный",
        "description": "Главный режим. Я решаю любые задачи: от идей и текстов до кода и стратегии.",
        "system_prompt": (
            "Ты — универсальный ИИ-ассистент BlackBox GPT. "
            "Отвечай максимально полезно, структурно и по делу. "
            "Пиши живым, но аккуратным языком, без воды. "
            "Если запрос неясен — сначала уточни, но не злоупотребляй вопросами."
        ),
    },
