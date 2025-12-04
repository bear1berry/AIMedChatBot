import asyncio
import logging
import os
import time
from typing import Tuple

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv

from .subscription_db import (
    get_active_premium_count,
    get_or_create_user,
    get_total_users_count,
    get_usage_stats,
    increment_usage,
    init_db,
)
from .payments_crypto import CryptoPayError, create_invoice

# ===================== Загрузка .env =====================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

FREE_MESSAGES_LIMIT = int(os.getenv("FREE_MESSAGES_LIMIT", "20"))

ADMIN_USERNAMES = {
    u.strip().lstrip("@").lower()
    for u in os.getenv("ADMIN_USERNAMES", "").split(",")
    if u.strip()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")

# Определяем провайдера LLM
if DEEPSEEK_API_KEY:
    LLM_PROVIDER = "deepseek"
elif GROQ_API_KEY:
    LLM_PROVIDER = "groq"
else:
    LLM_PROVIDER = None

# ===================== aiogram setup =====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# ===================== Клавиатуры =====================

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💡 Новый запрос")],
        [
            KeyboardButton(text="💎 Подписка"),
            KeyboardButton(text="👤 Профиль"),
        ],
        [KeyboardButton(text="ℹ️ Помощь")],
    ],
    resize_keyboard=True,
)

SUBSCRIPTION_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💎 Оформить подписку")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True,
)

# ===================== Вспомогательные функции =====================


def _is_admin(message: Message) -> bool:
    """Проверка, является ли пользователь админом по username."""
    if message.from_user is None:
        return False
    username = (message.from_user.username or "").lower()
    return username in ADMIN_USERNAMES


async def _ensure_user(message: Message) -> dict:
    """Гарантируем, что пользователь есть в БД, и возвращаем его запись."""
    return get_or_create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        is_admin=_is_admin(message),
    )


async def _check_access(message: Message) -> bool:
    """
    Проверяем:
    - настроены ли ключи LLM
    - не вышел ли бесплатный лимит
    - есть ли активный премиум
    """
    if LLM_PROVIDER is None:
        await message.answer(
            "⚠️ <b>Ключи для LLM не настроены.</b>\n"
            "Добавь в <code>.env</code> переменные "
            "<code>DEEPSEEK_API_KEY</code> или <code>GROQ_API_KEY</code>."
        )
        return False

    user = await _ensure_user(message)

    # Админ — всегда без ограничений
    if user["is_admin"]:
        return True

    now_ts = int(time.time())

    # Есть активный премиум
    premium_until = user["premium_until"]
    if premium_until and premium_until > now_ts:
        return True

    # Бесплатный лимит
    used = user["free_messages_used"] or 0
    if used < FREE_MESSAGES_LIMIT:
        return True

    # Лимит исчерпан — предлагаем подписку
    await message.answer(
        "🚫 Ты исчерпал лимит из "
        f"<b>{FREE_MESSAGES_LIMIT}</b> бесплатных сообщений.\n\n"
        "Чтобы продолжить пользоваться ботом без ограничений, "
        "оформи подписку 💎.",
        reply_markup=SUBSCRIPTION_KEYBOARD,
    )
    return False


async def _ask_llm(prompt: str) -> Tuple[str, int]:
    """
    Вызывает выбранную LLM и возвращает (ответ_модели, использованные_токены).
    """
    if LLM_PROVIDER is None:
        raise RuntimeError("LLM provider is not configured")

    if LLM_PROVIDER == "deepseek":
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        }
        model = os.getenv("DEEPSEEK_MODEL", "deepseek-reasoner")
    else:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        model = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")

    system_prompt = (
        "Ты умный и аккуратный ассистент.\n"
        "Отвечай структурированно, по делу и понятно.\n"
        "На медицинские вопросы обязательно добавляй дисклеймер, "
        "что это не заменяет очную консультацию врача."
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            url,
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    total_tokens = int(usage.get("total_tokens") or 0)
    return text, total_tokens


# ===================== Хендлеры =====================


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = await _ensure_user(message)

    if user["is_admin"]:
        role_text = "👑 Администратор"
    else:
        role_text = "🙋‍♂️ Пользователь"

    await message.answer(
        f"Привет, <b>{message.from_user.full_name}</b>!\n\n"
        "Это <b>AI Medicine Bot</b> — умный ассистент по медицине и не только.\n\n"
        f"Твой статус: {role_text}.\n\n"
        "Просто напиши вопрос или нажми «💡 Новый запрос».",
        reply_markup=MAIN_KEYBOARD,
    )


@dp.message(Command("help"))
@dp.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: Message) -> None:
    await message.answer(
        "❓ <b>Как пользоваться ботом</b>\n\n"
        "• Пиши любые вопросы — от здоровья и тренировок до технологий.\n"
        "• Первые сообщения — бесплатны, дальше нужна подписка 💎.\n"
        "• /start — главное меню.\n"
        "• /profile — твой статус и лимиты.\n"
        "• /admin — панель администратора (только для админов)."
    )


@dp.message(Command("profile"))
@dp.message(F.text == "👤 Профиль")
async def cmd_profile(message: Message) -> None:
    user = await _ensure_user(message)

    now_ts = int(time.time())
    premium_until = user["premium_until"]

    if user["is_admin"]:
        status = "👑 Администратор (безлимит)"
    elif premium_until and premium_until > now_ts:
        dt_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(premium_until))
        status = f"💎 Премиум до {dt_str}"
    else:
        remaining = max(FREE_MESSAGES_LIMIT - (user["free_messages_used"] or 0), 0)
        status = (
            "🆓 Бесплатный режим\n"
            f"Осталось бесплатных сообщений: <b>{remaining}</b>"
        )

    await message.answer(
        "👤 <b>Твой профиль</b>\n\n"
        f"ID: <code>{user['telegram_id']}</code>\n"
        f"Имя: {message.from_user.full_name}\n\n"
        f"Статус:\n{status}\n\n"
        f"Всего сообщений: <b>{user['total_messages']}</b>",
    )


@dp.message(Command("subscription"))
@dp.message(F.text.in_({"💎 Подписка", "💎 Оформить подписку"}))
async def cmd_subscription(message: Message) -> None:
    await _ensure_user(message)

    if not os.getenv("CRYPTO_PAY_API_TOKEN"):
        await message.answer(
            "⚠️ Платёжный провайдер не настроен.\n"
            "Добавь в <code>.env</code> переменную "
            "<code>CRYPTO_PAY_API_TOKEN</code> (токен от бота <b>@CryptoBot</b>)."
        )
        return

    await message.answer("Создаю счёт на оплату, секунду…")

    try:
        invoice = await create_invoice(message.from_user.id)
    except CryptoPayError as e:
        logging.exception("Failed to create invoice")
        await message.answer(
            "😔 Не удалось создать счёт на оплату. Попробуй позже.\n\n"
            f"Техническая информация: <code>{e}</code>"
        )
        return
    except Exception:
        logging.exception("Unexpected error while creating invoice")
        await message.answer(
            "😔 Не удалось создать счёт из-за непредвиденной ошибки. "
            "Попробуй ещё раз позже."
        )
        return

    pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")

    text = (
        "💎 <b>Подписка AI Medicine Premium</b>\n\n"
        "• Безлимитные запросы к ИИ\n"
        "• Приоритетная обработка\n"
        "• Фокус на медицине, технологиях и продуктивности\n\n"
        "Чтобы оплатить, просто перейди по ссылке 👇"
    )

    if pay_url:
        text += f"\n\n<a href=\"{pay_url}\">💳 Оплатить подписку</a>"
    else:
        text += "\n\nСсылка на оплату сейчас недоступна. Попробуй позже."

    await message.answer(text, reply_markup=MAIN_KEYBOARD)


@dp.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not _is_admin(message):
        await message.answer("⛔ Этот раздел доступен только администратору.")
        return

    total_users = get_total_users_count()
    premium_users = get_active_premium_count()
    usage = get_usage_stats()

    await message.answer(
        "👑 <b>Админ-панель</b>\n\n"
        f"Всего пользователей: <b>{total_users}</b>\n"
        f"Активных премиумов: <b>{premium_users}</b>\n\n"
        f"Всего сообщений через бота: <b>{usage['total_messages']}</b>\n"
        f"Суммарно токенов: <b>{usage['total_tokens']}</b>",
    )


@dp.message(F.text == "💡 Новый запрос")
async def handle_new_request_button(message: Message) -> None:
    await message.answer(
        "Пиши свой вопрос одним сообщением — чем конкретнее, тем лучше.\n"
        "Например:\n"
        "• «Разбери мои анализы…»\n"
        "• «Составь тренировку для…»\n"
        "• «Как прокачать продуктивность врачу в смены?»"
    )


@dp.message()
async def handle_chat(message: Message) -> None:
    # Игнорируем чистые команды (на них есть отдельные хендлеры)
    if message.text and message.text.startswith("/"):
        return

    allowed = await _check_access(message)
    if not allowed:
        return

    await message.answer("Думаю над ответом…")

    try:
        answer, tokens_used = await _ask_llm(message.text)
    except Exception:
        logging.exception("LLM error")
        await message.answer(
            "⚠️ Модель сейчас недоступна или вернула ошибку. "
            "Попробуй ещё раз чуть позже."
        )
        return

    await message.answer(answer, reply_markup=MAIN_KEYBOARD)

    # Обновляем статистику; если тут что-то упадёт — пользователю не мешаем
    try:
        increment_usage(message.from_user.id, tokens_used, count_free_message=True)
    except Exception:
        logging.exception("Failed to update usage stats")


# ===================== Точка входа =====================


async def main() -> None:
    init_db()
    logging.info("Starting AI Medicine bot…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
