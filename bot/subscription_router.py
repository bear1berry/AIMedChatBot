# bot/subscription_router.py

from __future__ import annotations

import logging
import os
from datetime import datetime

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

from .subscriptions import (
    MAIN_MENU_KEYBOARD,
    SUBSCRIPTION_KEYBOARD,
    MODE_SELECT_KEYBOARD,
    START_TEXT,
    LIMIT_REACHED_TEXT,
    SUBSCRIPTION_INFO_TEXT,
    HELP_TEXT,
    get_mode_key_from_button,
    get_mode_title,
    MODE_BUTTON_TEXTS,
)
from .subscription_db import (
    init_db,
    get_or_create_user,
    get_user,
    update_usage,
    grant_subscription,
    set_user_mode,
    get_stats,
)
from .payments_crypto import create_invoice, CryptoPayError

logger = logging.getLogger(__name__)

subscription_router = Router(name="subscription_router")

FREE_REQUESTS_LIMIT = int(os.getenv("FREE_REQUESTS_LIMIT", "3"))
FREE_TOKENS_LIMIT = int(os.getenv("FREE_TOKENS_LIMIT", "6000"))

# Администратор (владелец бота)
ADMIN_USERNAMES = {
    (os.getenv("ADMIN_USERNAME") or "bear1berry").lstrip("@").lower(),
}


def init_subscriptions_storage() -> None:
    init_db()
    logger.info("Subscription storage initialized")


def is_admin_user(telegram_id: int | None, username: str | None) -> bool:
    if username:
        uname = username.lstrip("@").lower()
        if uname in ADMIN_USERNAMES:
            return True
    # При желании можно добавить проверку по telegram_id через переменную окружения
    return False


async def check_user_access(message: Message) -> bool:
    """
    Проверка, можно ли сейчас отвечать пользователю.
    Админ и пользователи с активной подпиской — без ограничений.
    Остальные — по лимиту запросов и токенов.
    """
    if not message.from_user:
        return False

    user = get_or_create_user(message.from_user.id, message.from_user.username)

    # Админ всегда с полным доступом
    if is_admin_user(user.telegram_id, user.username):
        return True

    # Активная подписка — без ограничений
    if user.has_active_subscription:
        return True

    # Проверяем лимиты бесплатного режима
    if user.free_requests_used >= FREE_REQUESTS_LIMIT or user.free_tokens_used >= FREE_TOKENS_LIMIT:
        await message.answer(LIMIT_REACHED_TEXT, reply_markup=SUBSCRIPTION_KEYBOARD)
        return False

    return True


def register_successful_ai_usage(
    telegram_id: int,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """
    Фиксируем использование ИИ только для бесплатного режима.
    Для админа и подписчиков счётчик не увеличиваем.
    """
    user = get_user(telegram_id)
    if not user:
        return

    if is_admin_user(user.telegram_id, user.username):
        return

    if user.has_active_subscription:
        return

    total_tokens = max(0, input_tokens) + max(0, output_tokens)
    update_usage(telegram_id, add_requests=1, add_tokens=total_tokens)


def _build_profile_text(telegram_id: int) -> str:
    user = get_user(telegram_id)
    if not user:
        return "Профиль не найден. Попробуй ещё раз."

    mode_title = get_mode_title(user.current_mode)

    if user.has_active_subscription:
        paid_until_dt = datetime.fromtimestamp(user.paid_until or 0)
        sub_status = f"💎 Подписка активна до <b>{paid_until_dt:%d.%m.%Y}</b>"
    else:
        if user.free_requests_used == 0:
            sub_status = "🧪 Бесплатный доступ: ещё ни одного запроса не использовано"
        elif user.free_requests_used < FREE_REQUESTS_LIMIT:
            sub_status = (
                "🧪 Бесплатный доступ: "
                f"<b>{FREE_REQUESTS_LIMIT - user.free_requests_used}</b> запрос(ов) осталось"
            )
        else:
            sub_status = "⛔ Бесплатный лимит исчерпан"

    text_lines = [
        "<b>Твой мини-кабинет</b>",
        "",
        f"Режим: <b>{mode_title}</b>",
        "",
        sub_status,
        "",
        f"Запросы: <b>{user.free_requests_used}</b> из {FREE_REQUESTS_LIMIT} в бесплатном режиме",
        f"Токены: <b>{user.free_tokens_used}</b> из {FREE_TOKENS_LIMIT} (примерная оценка)",
        "",
        "Если хочешь стабильный доступ без ограничений — оформи премиум за 5$ в TON или USDT.",
        "Нажми «💎 Подписка» внизу, чтобы открыть экран с оплатой.",
    ]

    return "\n".join(text_lines)


# ---- ХЕНДЛЕРЫ ----


@subscription_router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    if not message.from_user:
        return
    text = _build_profile_text(message.from_user.id)
    await message.answer(text, reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(Command("faq"))
async def cmd_faq(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(F.text == "❓ Помощь")
async def on_help_button(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(F.text == "💎 Подписка")
async def on_subscription_button(message: Message) -> None:
    await message.answer(SUBSCRIPTION_INFO_TEXT, reply_markup=SUBSCRIPTION_KEYBOARD)


@subscription_router.message(F.text == "🔄 Перезапуск")
async def on_restart_button(message: Message) -> None:
    await message.answer("Диалог очищен. Можем начинать с чистого листа.", reply_markup=MAIN_MENU_KEYBOARD)
    await message.answer(START_TEXT, reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(F.text == "⬅️ Назад в меню")
async def on_back_to_menu(message: Message) -> None:
    await message.answer("Возвращаю в главное меню.", reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(F.text == "✨ Режим")
async def on_mode_button(message: Message) -> None:
    if not message.from_user:
        return

    user = get_or_create_user(message.from_user.id, message.from_user.username)
    mode_title = get_mode_title(user.current_mode)

    text = (
        "<b>Режимы работы бота</b>\n\n"
        "Режим определяет стиль и глубину моих ответов.\n"
        "Можешь переключать их в любой момент.\n\n"
        f"Текущий режим: <b>{mode_title}</b>.\n\n"
        "Выбери режим ниже — я подстроюсь под задачу."
    )
    await message.answer(text, reply_markup=MODE_SELECT_KEYBOARD)


@subscription_router.message(F.text.in_(MODE_BUTTON_TEXTS))
async def on_mode_selected(message: Message) -> None:
    if not message.from_user:
        return

    mode_key = get_mode_key_from_button(message.text or "")
    if not mode_key:
        await message.answer("Не удалось распознать режим. Попробуй ещё раз.", reply_markup=MODE_SELECT_KEYBOARD)
        return

    set_user_mode(message.from_user.id, mode_key)
    title = get_mode_title(mode_key)

    text = (
        f"Режим обновлён: <b>{title}</b>.\n\n"
        "Теперь мои ответы будут подстраиваться под этот фокус.\n"
        "Можешь в любой момент снова открыть «✨ Режим» и сменить формат."
    )
    await message.answer(text, reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(F.text == "Подписка на 30 дней — TON")
async def on_buy_ton(message: Message) -> None:
    await _handle_buy_plan(message, currency="TON")


@subscription_router.message(F.text == "Подписка на 30 дней — USDT")
async def on_buy_usdt(message: Message) -> None:
    await _handle_buy_plan(message, currency="USDT")


async def _handle_buy_plan(message: Message, currency: str) -> None:
    if not message.from_user:
        return

    amount = 5.0  # 5$ в выбранной валюте (TON или USDT)

    try:
        invoice_url = await create_invoice(
            amount=amount,
            currency=currency,
            description="AI Medicine — премиум-доступ на 30 дней",
            payer_username=message.from_user.username,
        )
    except CryptoPayError:
        logger.exception("Не удалось создать счёт через Crypto Pay")
        await message.answer(
            "Сейчас не удалось создать платёжный счёт через @CryptoBot.\n"
            "Попробуй ещё раз чуть позже.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        return

    await message.answer(
        "Я создал для тебя платёжный счёт через @CryptoBot.\n\n"
        f"Нажми по ссылке ниже, чтобы оплатить <b>5 {currency}</b> за 30 дней доступа:\n"
        f"{invoice_url}\n\n"
        "После оплаты вернись в бота — доступ будет активирован автоматически в течение пары секунд.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


@subscription_router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not message.from_user:
        return

    username = message.from_user.username
    if not is_admin_user(message.from_user.id, username):
        await message.answer("Эта команда доступна только владельцу бота.")
        return

    stats = get_stats()
    total_users = stats["total_users"]
    active_premium = stats["active_premium_users"]
    estimated_mrr = active_premium * 5

    text_lines = [
        "<b>Админ-панель</b>",
        "",
        f"Всего пользователей: <b>{total_users}</b>",
        f"Активных премиум-подписок: <b>{active_premium}</b>",
        "",
        f"Оценочный текущий MRR: <b>{estimated_mrr}$</b> в эквиваленте (5$ × премиум-подписки).",
        "",
        "Дальше можно добавить: список последних оплат, выгрузку активных пользователей и т.д.",
    ]

    await message.answer("\n".join(text_lines), reply_markup=MAIN_MENU_KEYBOARD)
