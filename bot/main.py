import asyncio
import logging
import os
import time
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)

from dotenv import load_dotenv

from .subscription_db import (
    init_db,
    get_or_create_user,
    get_user_by_telegram_id,
    increment_usage,
    get_usage_stats,
    get_total_users_count,
    get_active_premium_count,
)
from .payments_crypto import create_invoice, CryptoPayError

import httpx

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# --- Env & constants ---

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Укажи BOT_TOKEN в .env")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

FREE_MESSAGES_LIMIT = int(os.getenv("FREE_MESSAGES_LIMIT", "3"))
ADMIN_USERNAMES = {
    u.strip().lstrip("@")
    for u in os.getenv("ADMIN_USERNAMES", "").split(",")
    if u.strip()
}

LLM_PROVIDER: Optional[str]
if DEEPSEEK_API_KEY:
    LLM_PROVIDER = "deepseek"
elif GROQ_API_KEY:
    LLM_PROVIDER = "groq"
else:
    LLM_PROVIDER = None

if LLM_PROVIDER is None:
    logger.warning(
        "Ключи для LLM не настроены. Добавь в .env переменные DEEPSEEK_API_KEY или GROQ_API_KEY."
    )

# --- LLM client ---


async def call_llm(prompt: str) -> Tuple[str, int]:
    """
    Возвращает (ответ, использовано_токенов).
    """
    if LLM_PROVIDER is None:
        raise RuntimeError(
            "Ключи для LLM не настроены. Добавь DEEPSEEK_API_KEY или GROQ_API_KEY в .env"
        )

    messages = [
        {
            "role": "system",
            "content": (
                "Ты умный, внимательный и доброжелательный ассистент. "
                "Отвечай структурировано и по делу, без лишней воды. "
                "Поддерживай стиль живого, но уважительного диалога."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    if LLM_PROVIDER == "deepseek":
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        }
        url = "https://api.deepseek.com/chat/completions"
        payload = {
            "model": "deepseek-chat",
            "messages": messages,
            "stream": False,
        }
    else:  # groq
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": "gpt-4o-mini",
            "messages": messages,
            "stream": False,
        }

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()

    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    total_tokens = usage.get("total_tokens") or (
        usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    )
    return text, int(total_tokens)


# --- Keyboards ---


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🧠 Пространства"),
                KeyboardButton(text="💎 Подписка"),
            ],
            [
                KeyboardButton(text="🆘 Помощь"),
                KeyboardButton(text="🔄 Перезапуск"),
            ],
        ],
        resize_keyboard=True,
    )


# --- Access control & helpers ---


def _is_admin(username: Optional[str]) -> bool:
    if not username:
        return False
    return username.lstrip("@") in ADMIN_USERNAMES


async def check_access(message: Message) -> bool:
    """
    Проверяет, можно ли сейчас отвечать пользователю.
    Обновляет/создаёт запись пользователя.
    """
    from_user = message.from_user
    assert from_user is not None

    is_admin = _is_admin(from_user.username)
    user = get_or_create_user(
        telegram_id=from_user.id,
        username=from_user.username or "",
        is_admin=is_admin,
    )

    # Админам всегда можно всё
    if user["is_admin"]:
        return True

    # Активный премиум?
    now_ts = int(time.time())
    premium_until = user.get("premium_until") or 0
    if premium_until and premium_until > now_ts:
        return True

    # Лимит бесплатных сообщений
    if user["free_messages_used"] >= FREE_MESSAGES_LIMIT:
        await message.answer(
            "Ты уже использовал свои бесплатные запросы.\n\n"
            "Чтобы продолжить работу в премиальном режиме без жёстких ограничений, "
            "нажми кнопку «💎 Подписка» внизу.",
        )
        return False

    return True


# --- Routers & handlers ---

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    from_user = message.from_user
    assert from_user is not None

    is_admin = _is_admin(from_user.username)
    user = get_or_create_user(
        telegram_id=from_user.id,
        username=from_user.username or "",
        is_admin=is_admin,
    )

    text_lines = [
        "Привет 👋",
        "",
        f"У тебя есть {FREE_MESSAGES_LIMIT} бесплатных запросов, чтобы протестировать бота.",
        "Дальше — премиальный режим без жёстких ограничений.",
        "",
        "Команды:",
        "• /profile — твой мини-кабинет",
        "• /faq — ответы на часто задаваемые вопросы",
    ]
    if user["is_admin"]:
        text_lines.append("• /admin — панель управления (только для администратора)")

    text_lines.append("")
    text_lines.append("Просто напиши свой первый запрос 👇")

    await message.answer("\n".join(text_lines), reply_markup=main_menu_kb())


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    from_user = message.from_user
    assert from_user is not None

    user = get_user_by_telegram_id(from_user.id)
    if not user:
        await message.answer("Профиль пока не найден. Напиши любое сообщение боту.")
        return

    now_ts = int(time.time())
    premium_until = user.get("premium_until") or 0
    if user["is_admin"]:
        premium_status = "Администратор — всегда полный доступ 🔥"
    elif premium_until and premium_until > now_ts:
        dt_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(premium_until))
        premium_status = f"Активная подписка до {dt_str}"
    else:
        premium_status = "Подписка не активна"

    used = user["free_messages_used"]
    text = (
        "<b>Твой профиль</b>\n\n"
        f"🆔 ID: <code>{user['telegram_id']}</code>\n"
        f"👤 Username: @{user['username'] or 'без ника'}\n\n"
        f"💎 Статус: {premium_status}\n"
        f"💬 Бесплатные сообщения: {used} из {FREE_MESSAGES_LIMIT}\n"
    )
    await message.answer(text)


@router.message(Command("faq"))
async def cmd_faq(message: Message) -> None:
    text = (
        "<b>FAQ — ответы на частые вопросы</b>\n\n"
        "• <b>Что за бесплатные запросы?</b>\n"
        f"У тебя есть {FREE_MESSAGES_LIMIT} сообщений, чтобы почувствовать бота.\n\n"
        "• <b>Что даёт премиум?</b>\n"
        "Нет жёстких ограничений по длине и глубине ответов, приоритетная обработка "
        "и максимум пользы в каждом разборе.\n\n"
        "• <b>Как оплатить?</b>\n"
        "Нажми кнопку «💎 Подписка» внизу и выбери удобный способ оплаты через Crypto Bot "
        "(TON / USDT)."
    )
    await message.answer(text)


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    from_user = message.from_user
    assert from_user is not None

    if not _is_admin(from_user.username):
        await message.answer("Эта команда доступна только администратору.")
        return

    total = get_total_users_count()
    premium = get_active_premium_count()
    usage = get_usage_stats()

    text = (
        "<b>Админ-панель</b>\n\n"
        f"👥 Всего пользователей: {total}\n"
        f"💎 Активных премиумов: {premium}\n\n"
        f"📊 Сообщений всего: {usage['total_messages']}\n"
        f"📊 Токенов всего: {usage['total_tokens']}\n"
    )
    await message.answer(text)


@router.message(F.text == "🔄 Перезапуск")
async def on_restart(message: Message) -> None:
    await cmd_start(message)


@router.message(F.text == "🆘 Помощь")
async def on_help_button(message: Message) -> None:
    await cmd_faq(message)


@router.message(F.text == "🧠 Пространства")
async def on_spaces(message: Message) -> None:
    await message.answer(
        "Скоро здесь появятся специальные режимы и пространства.\n"
        "Пока просто задай любой вопрос — я разберу ситуацию максимально глубоко.",
    )


@router.message(F.text == "💎 Подписка")
async def on_subscription(message: Message) -> None:
    from_user = message.from_user
    assert from_user is not None

    try:
        invoice_url = await create_invoice(
            user_id=from_user.id,
            plan_code="premium_30d",
        )
    except CryptoPayError:
        await message.answer(
            "Не удалось создать счёт на оплату. Попробуй чуть позже 🙏\n"
            "Если ошибка повторяется — свяжись с владельцем бота.",
        )
        return

    text = (
        "<b>Premium-доступ</b>\n\n"
        "Режим без жёстких лимитов по длине и глубине ответов.\n"
        "Ты задаёшь вопрос — я разбираю ситуацию до основания и выдаю максимум пользы.\n\n"
        "Подписка на 30 дней:\n"
        "• Полный доступ ко всем возможностям бота\n"
        "• Длинные запросы, глубокие разборы\n"
        "• Приоритетная обработка\n\n"
        "Стоимость: 5 TON или 5 USDT в месяц.\n\n"
        "Нажми на кнопку ниже, чтобы оплатить через Crypto Bot 👇"
    )
    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await message.answer(invoice_url, reply_markup=main_menu_kb())


@router.message()
async def handle_ai(message: Message) -> None:
    if LLM_PROVIDER is None:
        await message.answer(
            "⚠️ Ключи для LLM не настроены.\n"
            "Добавь в <code>.env</code> переменные <code>DEEPSEEK_API_KEY</code> или "
            "<code>GROQ_API_KEY</code> и перезапусти бота."
        )
        return

    if not await check_access(message):
        return

    from_user = message.from_user
    assert from_user is not None

    await message.chat.do("typing")

    try:
        answer, tokens_used = await call_llm(message.text)
    except Exception:
        logger.exception("LLM error")
        await message.answer(
            "Произошла ошибка при обращении к модели. Попробуй ещё раз чуть позже."
        )
        return

    # учёт использования
    increment_usage(
        telegram_id=from_user.id,
        messages_delta=1,
        tokens_delta=tokens_used,
    )

    await message.answer(answer)


async def main() -> None:
    init_db()
    dp = Dispatcher()
    dp.include_router(router)

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
