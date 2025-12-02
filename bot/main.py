from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config import settings
from .handlers import router as main_router


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main() -> None:
    """
    Точка входа бота.

    Важно: используем HTML parse_mode, чтобы <b>, <i>, <code> и аккуратная
    разметка из промптов отображались в Telegram как задумано.
    """
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(main_router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
