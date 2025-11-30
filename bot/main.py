from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher

from .config import settings
from .handlers import router as main_router
from .memory import _get_conn  # noqa: F401  # side-effect init DB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)


async def main() -> None:
    if not settings.bot_token:
        logger.error("BOT_TOKEN is not set")
        raise SystemExit("BOT_TOKEN is required")

    bot = Bot(token=settings.bot_token, parse_mode="HTML")
    dp = Dispatcher()
    dp.include_router(main_router)

    logger.info("Bot started. Waiting for messages...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
