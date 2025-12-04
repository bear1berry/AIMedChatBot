# bot/main.py

import asyncio
import logging
import os
from typing import Any, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message

from .subscription_router import (
    router as subscription_router,
    init_subscriptions_storage,
    check_user_access,
    register_successful_ai_usage,
)

# Пытаемся импортировать твой реальный клиент ИИ.
# Ожидается async-функция:
# async def ask_ai(user_text: str, user_id: int) -> str | tuple[str, int, int]
try:
    from .llm_client import ask_ai  # type: ignore
except ImportError:
    async def ask_ai(user_text: str, user_id: int) -> str:
        # Простая заглушка, чтобы код не падал
        return f"Эхо: {user_text}"


BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Укажи BOT_TOKEN или TELEGRAM_BOT_TOKEN в переменных окружения / .env")


dp = Dispatcher()
dp.include_router(subscription_router)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    text = (
        "Привет 👋\n\n"
        "У тебя есть <b>3 бесплатных запроса</b>, чтобы протестировать бота.\n"
        "Дальше — премиальный доступ с оплатой в TON / USDT.\n\n"
        "Команды:\n"
        "• /profile — твой мини-кабинет\n"
        "• /faq — ответы на вопросы по подписке\n\n"
        "Просто напиши свой первый запрос."
    )
    await message.answer(text)


@dp.message(F.text)
async def handle_ai(message: Message):
    """
    Основной хендлер, который отправляет запрос в модель ИИ
    и учитывает лимиты подписки.
    """
    # 1. Проверка доступа (подписка / лимиты)
    if not await check_user_access(message):
        return

    user_id = message.from_user.id
    user_text = (message.text or "").strip()
    if not user_text:
        return

    # 2. Запрос к модели
    result: Any = await ask_ai(user_text, user_id=user_id)

    # Поддержка двух вариантов:
    # - ask_ai -> str
    # - ask_ai -> (str, input_tokens, output_tokens)
    reply_text: str
    input_tokens: int | None = None
    output_tokens: int | None = None

    if isinstance(result, tuple):
        # берём максимум первые три элемента
        reply_text = str(result[0])
        if len(result) > 1:
            try:
                input_tokens = int(result[1])
            except Exception:
                input_tokens = None
        if len(result) > 2:
            try:
                output_tokens = int(result[2])
            except Exception:
                output_tokens = None
    else:
        reply_text = str(result)

    # Если usage не пришёл — оцениваем по длине текста
    if input_tokens is None:
        input_tokens = max(1, len(user_text) // 4)
    if output_tokens is None:
        output_tokens = max(1, len(reply_text) // 4)

    # 3. Отправляем ответ
    await message.answer(reply_text)

    # 4. Фиксируем использование лимитов
    register_successful_ai_usage(
        telegram_id=user_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def main():
    logging.basicConfig(level=logging.INFO)
    # Инициализируем БД подписок
    init_subscriptions_storage()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
