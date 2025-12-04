# bot/main.py

import asyncio
import logging
import os
import time
import contextlib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message

from .subscriptions import (
    MAIN_MENU_KEYBOARD,
    START_TEXT,
    ALL_BUTTON_TEXTS,
    get_mode_prompt,
    get_mode_title,
)
from .subscription_router import (
    subscription_router,
    init_subscriptions_storage,
    check_user_access,
    register_successful_ai_usage,
    is_admin_user,
    FREE_REQUESTS_LIMIT,
    FREE_TOKENS_LIMIT,
)
from .subscription_db import (
    get_user,
    get_pending_payments,
    mark_payment_paid,
    grant_subscription,
)
from .payments_crypto import fetch_invoices_statuses

# Путь к .env относительно файла main.py
BASE_DIR = Path(__file__).resolve().parent.parent
env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=env_path, override=True)

# Пытаемся импортировать реальный клиент ИИ
try:
    from .llm_client import ask_ai  # type: ignore
except ImportError:
    async def ask_ai(user_text: str, user_id: int) -> str:
        return f"Эхо: {user_text}"


BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        f"Укажи BOT_TOKEN или TELEGRAM_BOT_TOKEN в {env_path} "
        "или в переменных окружения."
    )

dp = Dispatcher()
dp.include_router(subscription_router)

# Тексты, которые НЕ нужно отправлять в ИИ (кнопки и служебные надписи)
BLOCKED_TEXTS = set(ALL_BUTTON_TEXTS)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(START_TEXT, reply_markup=MAIN_MENU_KEYBOARD)


@dp.message(F.text & ~F.text.in_(BLOCKED_TEXTS))
async def handle_ai(message: Message):
    """
    Основной хендлер для общения с ИИ.
    Лимиты и подписки проверяются через check_user_access().
    """
    if not await check_user_access(message):
        return

    if not message.from_user:
        return

    user_id = message.from_user.id
    user_text = (message.text or "").strip()
    if not user_text:
        return

    # Профиль поль
