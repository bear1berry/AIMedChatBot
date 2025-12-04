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

# Лимиты из .env
FREE_REQUESTS_LIMIT = int(os.getenv("FREE_REQUESTS_LIMIT", "3"))
FREE_TOKENS_LIMIT = int(os.getenv("FREE_TOKENS_LIMIT", "6000"))

# Подписка: цена и длительность
SUBSCRIPTION_DAYS = 30
SUBSCRIPTION_PRICE_TON = 5.0
SUBSCRIPTION_PRICE_USDT = 5.0


# ---------- ИНИЦИАЛИЗАЦИЯ БД ----------


def init_subscriptions_storage() -> None:
    init_db()
    logging.info("Subscription storage initialized")


# ---------- УТИЛИТЫ ----------


def _format_date(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def _build_profile_text(user: UserSubscription) -> str:
    lines = ["<b>Твой мини-кабинет</b>\n"]
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


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ main.py ----------


async def check_user_access(message: Message) -> bool:
    """True — можно отвечать ИИ, False — нужно показывать экран подписки."""
    from_user = message.from_user
    if not from_user:
        return False

    user = get_or_create_user(from_user.id, from_user.username)

    if user.has_active_subscription:
        return True

    # Проверяем лимиты
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
    """Регистрируем использование ИИ для бесплатного режима."""
    user = get_user(telegram_id) or get_or_create_user(telegram_id)

    if user.has_active_subscription:
        # Для платников бесплатные лимиты не трогаем
        return

    total_tokens = max(0, int(input_tokens) + int(output_tokens))
    add_free_usage(telegram_id, add_requests=1, add_tokens=total_tokens)


# ---------- ХЕНДЛЕРЫ ----------


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
    user_id = message.from_user.id

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
        logging.exception("Не удалось создать счёт через Crypto Pay")
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

    # Простой вариант: считаем, что подписка активна после выдачи счёта.
    # (Если захочешь — потом сделаем вебхук по факту оплаты.)
    grant_subscription(user_id, SUBSCRIPTION_DAYS)
