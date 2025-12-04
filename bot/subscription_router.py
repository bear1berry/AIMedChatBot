# bot/subscription_router.py

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from .subscriptions import (
    MAIN_MENU_KEYBOARD,
    SUBSCRIPTION_KEYBOARD,
    START_TEXT,
    LIMIT_REACHED_TEXT,
    SUBSCRIPTION_INFO_TEXT,
    HELP_TEXT,
)
from .subscription_db import (
    init_db,
    get_or_create_user,
    get_user,
    add_free_usage,
    grant_subscription,
    UserSubscription,
)
from .payments_crypto import create_invoice, CryptoPayError

subscription_router = Router()

# ---------- НАСТРОЙКИ И ЛИМИТЫ ----------

FREE_REQUESTS_LIMIT = int(os.getenv("FREE_REQUESTS_LIMIT", "3"))
FREE_TOKENS_LIMIT = int(os.getenv("FREE_TOKENS_LIMIT", "6000"))

SUBSCRIPTION_DAYS = 30
SUBSCRIPTION_PRICE_TON = 5.0
SUBSCRIPTION_PRICE_USDT = 5.0

# Админы по username (берём из .env, без @, регистр игнорируем)
ADMIN_USERNAMES = {
    name.lstrip("@").lower()
    for name in os.getenv("ADMIN_USERNAMES", "").replace(",", " ").split()
    if name.strip()
}


def is_admin_user(telegram_id: int, username: Optional[str]) -> bool:
    """
    Проверяем, является ли пользователь админом.
    Сейчас привязка по username (без @).
    При желании можно расширить на ID.
    """
    if not ADMIN_USERNAMES:
        return False
    if not username:
        return False
    return username.lstrip("@").lower() in ADMIN_USERNAMES


# ---------- ИНИЦИАЛИЗАЦИЯ БД ----------


def init_subscriptions_storage() -> None:
    init_db()
    logging.info("Subscription storage initialized")


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------


def _format_date(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def _build_profile_text(user: UserSubscription) -> str:
    lines: list[str] = []

    if is_admin_user(user.telegram_id, user.username):
        lines.append("<b>Твой мини-кабинет</b>\n")
        lines.append("Роль: администратор 🔥")
        lines.append("Статус: полный доступ без ограничений.\n")
        lines.append(
            f"Бесплатные запросы (для статистики): {user.free_requests_used}/{FREE_REQUESTS_LIMIT}"
        )
        lines.append(
            f"Бесплатные токены (для статистики): {user.free_tokens_used}/{FREE_TOKENS_LIMIT}"
        )
        return "\n".join(lines)

    lines.append("<b>Твой мини-кабинет</b>\n")

    if user.has_active_subscription:
        lines.append("Статус: активная подписка ✅")
        lines.append(f"Доступ до: <code>{_format_date(user.paid_until)}</code>")
    else:
        lines.append("Статус: без активной подписки")

    lines.append("")
    lines.append(
        f"Бесплатные запросы: {user.free_requests_used}/{FREE_REQUESTS_LIMIT}"
    )
    lines.append(
        f"Бесплатные токены: {user.free_tokens_used}/{FREE_TOKENS_LIMIT}"
    )
    return "\n".join(lines)


# ---------- ФУНКЦИИ ДЛЯ main.py ----------


async def check_user_access(message: Message) -> bool:
    """
    True — можно отправлять запрос в ИИ,
    False — нужно показать экран подписки / лимитов.
    """
    from_user = message.from_user
    if not from_user:
        return False

    user = get_or_create_user(from_user.id, from_user.username)

    # Админ всегда с полным доступом, вообще без ограничений
    if is_admin_user(user.telegram_id, user.username):
        return True

    if user.has_active_subscription:
        return True

    # Проверяем лимиты бесплатного режима
    limit_requests = user.free_requests_used >= FREE_REQUESTS_LIMIT
    limit_tokens = user.free_tokens_used >= FREE_TOKENS_LIMIT

    if limit_requests or limit_tokens:
        await message.answer(LIMIT_REACHED_TEXT, reply_markup=MAIN_MENU_KEYBOARD)
        return False

    return True


def register_successful_ai_usage(
    *,
    telegram_id: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """
    Регистрируем использование ИИ в рамках бесплатных лимитов.
    Для админа ничего не считаем.
    """
    user = get_user(telegram_id) or get_or_create_user(telegram_id)

    if is_admin_user(user.telegram_id, user.username):
        # Админ — без ограничений, счётчики не трогаем
        return

    if user.has_active_subscription:
        # Для оплаченной подписки бесплатные лимиты не считаем
        return

    total_tokens = max(0, int(input_tokens) + int(output_tokens))
    add_free_usage(telegram_id, add_requests=1, add_tokens=total_tokens)


# ---------- КОМАНДЫ И КНОПКИ ----------


@subscription_router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    user = get_or_create_user(message.from_user.id, message.from_user.username)
    text = _build_profile_text(user)
    await message.answer(text, reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(Command("faq"))
@subscription_router.message(F.text == "❓ Помощь")
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(F.text == "💎 Подписка")
async def cmd_subscription_menu(message: Message) -> None:
    from_user = message.from_user
    if from_user and is_admin_user(from_user.id, from_user.username):
        await message.answer(
            "Ты админ этого бота, у тебя уже полный доступ без каких-либо ограничений 😉\n\n"
            "Если хочешь протестировать оплату — выбери вариант ниже и создай счёт.",
            reply_markup=SUBSCRIPTION_KEYBOARD,
        )
        return

    await message.answer(SUBSCRIPTION_INFO_TEXT, reply_markup=SUBSCRIPTION_KEYBOARD)


@subscription_router.message(F.text == "⬅️ Назад в меню")
async def cmd_back_to_menu(message: Message) -> None:
    await message.answer("Вернул тебя в главное меню.", reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(F.text == "🔄 Перезапуск")
async def cmd_restart(message: Message) -> None:
    await message.answer(START_TEXT, reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(F.text == "Подписка на 30 дней — TON")
async def cmd_buy_ton(message: Message) -> None:
    await _handle_buy_plan(message, asset="TON", price=SUBSCRIPTION_PRICE_TON)


@subscription_router.message(F.text == "Подписка на 30 дней — USDT")
async def cmd_buy_usdt(message: Message) -> None:
    await _handle_buy_plan(message, asset="USDT", price=SUBSCRIPTION_PRICE_USDT)


async def _handle_buy_plan(message: Message, asset: str, price: float) -> None:
    from_user = message.from_user
    if not from_user:
        return

    user_id = from_user.id

    await message.answer("Создаю счёт на оплату…", reply_markup=SUBSCRIPTION_KEYBOARD)

    payload = f"user:{user_id}|plan:30d|asset:{asset}"
    description = "Подписка на 30 дней для AI Medicine Bot"

    try:
        invoice_url = await create_invoice(
            asset=asset,
            amount=price,
            description=description,
            payload=payload,
        )
    except CryptoPayError as e:
        logging.error("Не удалось создать счёт через Crypto Pay: %s", e)
        await message.answer(
            "Не удалось создать счёт на оплату. Попробуй чуть позже 🙏\n"
            "Если ошибка повторяется — свяжись с владельцем бота.",
            reply_markup=SUBSCRIPTION_KEYBOARD,
        )
        return

    text = (
        f"Счёт на <b>{price} {asset}</b> создан ✅\n\n"
        "Оплата проходит через официальный @CryptoBot.\n"
        "Нажми по ссылке ниже, чтобы открыть счёт и оплатить:\n\n"
        f"{invoice_url}"
    )
    await message.answer(text, reply_markup=SUBSCRIPTION_KEYBOARD)

    # Продление подписки для обычных пользователей.
    # Админу подписка не нужна, но создавать счёт он всё равно может (для тестов).
    if not is_admin_user(from_user.id, from_user.username):
        grant_subscription(user_id, SUBSCRIPTION_DAYS)
