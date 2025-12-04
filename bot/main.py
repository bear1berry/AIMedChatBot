from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from dotenv import load_dotenv

from .subscription_router import (
    subscription_router,
    check_user_access,
    show_subscription_menu,
    build_main_menu,
    is_admin_username,
)
from .subscription_db import init_db

logger = logging.getLogger(__name__)


# ---------------------- LLM client ----------------------


SYSTEM_PROMPT = (
    "Ты — личный ассистент и экспертный AI-напарник. "
    "Отвечай по делу, структурировано и человеческим языком. "
    "Если вопрос медицинский, помни: ты не заменяешь очного врача."
)


class LLMClient:
    def __init__(self) -> None:
        self.deepseek_api_key = os.getenv("DEEPSEEK_API_KEY")
        self.deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.groq_model = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")

    @property
    def provider(self) -> Optional[str]:
        if self.deepseek_api_key:
            return "deepseek"
        if self.groq_api_key:
            return "groq"
        return None

    async def generate(self, user_text: str, user_id: int) -> str:
        if not self.provider:
            return (
                "⚠️ Ключи для LLM не настроены.\n"
                "Добавь в <code>.env</code> переменные "
                "<code>DEEPSEEK_API_KEY</code> или <code>GROQ_API_KEY</code>."
            )

        if self.provider == "deepseek":
            return await self._call_deepseek(user_text, user_id)
        if self.provider == "groq":
            return await self._call_groq(user_text, user_id)
        return "⚠️ Не удалось выбрать провайдера LLM."

    async def _call_deepseek(self, user_text: str, user_id: int) -> str:
        url = "https://api.deepseek.com/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.deepseek_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            "temperature": 0.7,
            "max_tokens": 1024,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=headers)

        try:
            data = resp.json()
        except Exception:
            logger.exception("DeepSeek invalid JSON: %s", resp.text)
            return "⚠️ Не удалось получить ответ от модели DeepSeek."

        try:
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            logger.error("DeepSeek unexpected response: %s", data)
            return "⚠️ Модель вернула неожиданный ответ."

    async def _call_groq(self, user_text: str, user_id: int) -> str:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.groq_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            "temperature": 0.7,
            "max_tokens": 1024,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload, headers=headers)

        try:
            data = resp.json()
        except Exception:
            logger.exception("Groq invalid JSON: %s", resp.text)
            return "⚠️ Не удалось получить ответ от модели Groq."

        try:
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            logger.error("Groq unexpected response: %s", data)
            return "⚠️ Модель вернула неожиданный ответ."


llm_client = LLMClient()


# ---------------------- Bot setup ----------------------


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN или TELEGRAM_BOT_TOKEN в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
dp.include_router(subscription_router)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """
    Приветственный экран.
    """
    is_admin = is_admin_username(message.from_user.username)
    kb = build_main_menu(is_admin=is_admin)

    text = (
        "👋 Привет, я <b>Alexander / AI Medicine бот</b>.\n\n"
        "• Отвечаю на умные вопросы про жизнь, здоровье, технологии и прокачку себя.\n"
        "• Первые запросы — бесплатные, дальше включается премиум-режим.\n\n"
        "Просто напиши свой вопрос или нажми нужную кнопку внизу."
    )
    await message.answer(text, reply_markup=kb)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "❓ <b>Как пользоваться ботом</b>\n\n"
        "• Пиши любой вопрос — отвечу как умный ассистент.\n"
        "• Для просмотра статуса и оформления подписки используй кнопку «⭐ Подписка».\n"
        "• Админ-панель: /admin или кнопка «🛠 Админ-панель» (только для админа)."
    )


@dp.message(F.text == "⭐ Подписка")
async def msg_subscription(message: Message) -> None:
    await show_subscription_menu(message)


@dp.message(F.text == "🛠 Админ-панель")
async def msg_admin_shortcut(message: Message) -> None:
    # Реальная логика админки — в subscription_router, здесь просто чтобы текст не ушёл в общий обработчик
    pass


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_ai_chat(message: Message) -> None:
    """
    Главный обработчик AI-диалога.
    """
    if not await check_user_access(message):
        return

    await message.chat.do("typing")
    reply = await llm_client.generate(message.text or "", message.from_user.id)
    await message.answer(reply)


async def main() -> None:
    init_db()
    logger.info("Starting bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
