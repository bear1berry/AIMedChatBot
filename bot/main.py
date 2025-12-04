# bot/main.py

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message

from .subscriptions import MAIN_MENU_KEYBOARD, START_TEXT
from .subscription_router import (
    subscription_router,
    init_subscriptions_storage,
    check_user_access,
    register_successful_ai_usage,
)

# Пусть к .env относительно файла main.py
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

# Тексты, которые НЕ нужно отправлять в ИИ (кнопки)
BLOCKED_TEXTS = {
    "💎 Подписка",
    "❓ Помощь",
    "🔄 Перезапуск",
    "Подписка на 30 дней — TON",
    "Подписка на 30 дней — USDT",
    "⬅️ Назад в меню",
}


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

    user_id = message.from_user.id
    user_text = (message.text or "").strip()
    if not user_text:
        return

    result: Any = await ask_ai(user_text, user_id=user_id)

    reply_text: str
    input_tokens: int | None = None
    output_tokens: int | None = None

    if isinstance(result, tuple):
        reply_text = str(result[0])
        if len(result) > 1:
            try:
                input_tokens = int(result[1])
            except Exception:  # noqa: BLE001
                input_tokens = None
        if len(result) > 2:
            try:
                output_tokens = int(result[2])
            except Exception:  # noqa: BLE001
                output_tokens = None
    else:
        reply_text = str(result)

    if input_tokens is None:
        input_tokens = max(1, len(user_text) // 4)
    if output_tokens is None:
        output_tokens = max(1, len(reply_text) // 4)

    await message.answer(reply_text)

    register_successful_ai_usage(
        telegram_id=user_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def main():
    logging.basicConfig(level=logging.INFO)

    init_subscriptions_storage()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
