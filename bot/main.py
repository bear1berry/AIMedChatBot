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
from .subscription_db import get_user

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

    # Профиль пользователя и текущий режим
    user_profile = get_user(user_id)
    mode_key = user_profile.current_mode if user_profile else None
    mode_title = get_mode_title(mode_key)
    mode_prompt = get_mode_prompt(mode_key)

    # Строим вход для модели с учётом режима
    if mode_prompt:
        llm_input = (
            f"{mode_prompt}\n\n"
            f"Текущий режим: {mode_title}.\n\n"
            f"Вопрос пользователя:\n{user_text}"
        )
    else:
        llm_input = user_text

    # Запрос к модели
    result: Any = await ask_ai(llm_input, user_id=user_id)

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

    # Мягкое напоминание о лимитах для бесплатного режима
    await send_usage_hint(message)


async def send_usage_hint(message: Message) -> None:
    """
    Показываем мягкие подсказки по оставшимся бесплатным запросам,
    только для тех, у кого нет подписки и кто не админ.
    """
    from_user = message.from_user
    if not from_user:
        return

    user = get_user(from_user.id)
    if not user:
        return

    if is_admin_user(user.telegram_id, user.username):
        return
    if user.has_active_subscription:
        return

    remaining_requests = max(0, FREE_REQUESTS_LIMIT - user.free_requests_used)

    # Если запас большой — ничего не пишем
    if remaining_requests > 2:
        return

    if remaining_requests == 2:
        text = (
            "У тебя осталось ещё <b>2 бесплатных запроса</b>. "
            "Используй их с пользой 😉"
        )
    elif remaining_requests == 1:
        text = (
            "Это <b>последний бесплатный запрос</b>.\n\n"
            "Дальше — только в премиум-режиме 💎.\n"
            "Если не хочешь ограничений — нажми «💎 Подписка» внизу."
        )
    else:
        # Уже всё израсходовано — здесь ничего не говорим,
        # это обработает check_user_access в следующий раз.
        return

    await message.answer(text, reply_markup=MAIN_MENU_KEYBOARD)


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
