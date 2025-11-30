import asyncio
import logging

from aiogram import Bot, Dispatcher

from .config import settings
from .handlers import router


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()

    dp.include_router(router)

    logger.info("Бот запущен и ожидает сообщения...")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
