import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from .config import settings
from .handlers import router
from .memory import init_db


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("bot.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def main() -> None:
    setup_logging()
    init_db()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    dp = Dispatcher()
    dp.include_router(router)

    logging.getLogger(__name__).info("Bot started. Waiting for messages...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
