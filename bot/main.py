import asyncio
import logging
import os
import time
from typing import Tuple

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv

from .subscription_db import (
    get_active_premium_count,
    get_or_create_user,
    get_total_users_count,
    get_usage_stats,
    increment_usage,
    init_db,
)
from .payments_crypto import CryptoPayError, create_invoice

# Загружаем .env сразу при импорте
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

FREE_MESSAGES_LIMIT = int(os.getenv("FREE_MESSAGES_LIMIT", "20"))

ADMIN_USERNAMES = {
    u.strip().lstrip("@").lower()
    for u in os.getenv("ADMIN_USERNAMES", "").split(",")
    if u.strip()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")

# Определяем провайдера LLM
if DEEPSEEK_API_KEY:
    LLM_PROVIDER = "deepseek"
elif GROQ_API_KEY:
    LLM_PROVIDER = "groq"
else:
    LLM_PROVIDER = None

# ====== aiogram setup ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()


# ====== Keyboards ======
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💡 Новый запрос")],
        [
            KeyboardButton(text="💎 Подписка"),
            KeyboardButton(text="👤 Профиль"),
        ],
        [KeyboardButton(text="ℹ️ Помощь")],
    ],
    resize_keyboard=True,
)

SUBSCRIPTION_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💎 Оформить подписку")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True,
)


# ====== Helpers ======
def _is_admin(message: Message) -> bool:
    if message.from_user is None:
        return False
    username = (message.from_user.username or_
