# bot/main.py
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config import settings
from .handlers import router
from .memory import init_db


async def main() -> None:
    # Логирование
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logger = logging.getLogger(__name__)

    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")

    # Инициализация БД
    init_db()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    dp.include_router(router)

    logger.info("Bot started. Waiting for messages...")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
