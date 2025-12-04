from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Optional, Dict, Any, List

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
    get_premium_users,
    get_recent_payments,
    export_users_and_payments,
    create_payment,
    Payment,
)
from .payments_crypto import create_invoice, CryptoPayError

logger = logging.getLogger(__name__)

subscription_router = Router(name="subscription_router")

# Лимиты бесплатного режима
FREE_REQUESTS_LIMIT = int(os.getenv("FREE_REQUESTS_LIMIT", "3"))
FREE_TOKENS_LIMIT = int(os.getenv("FREE_TOKENS_LIMIT", "6000"))

# Админ (владелец бота)
ADMIN_USERNAMES = {
    (os.getenv("ADMIN_USERNAME") or "bear1berry").lstrip("@").lower(),
}


# --- ИНИЦИАЛИЗАЦИЯ ХРАНИЛИЩА ПОДПИСОК ---


def init_subscriptions_storage() -> None:
    init_db()
    logger.info("Subscription storage initialized")


def is_admin_user(telegram_id: Optional[int], username: Optional[str]) -> bool:
    """
    Проверяем, является ли пользователь админом по username.
    """
    if username:
        uname = username.lstrip("@").lower()
        if uname in ADMIN_USERNAMES:
            return True
    return False


async def check_user_access(message: Message) -> bool:
    """
    Проверка: можно ли сейчас отвечать пользователю на запрос к ИИ.
    Админ и активные подписчики — без лимитов.
    Остальные — по лимитам запросов/токенов.
    """
    if not message.from_user:
        return False

    user = get_or_create_user(message.from_user.id, message.from_user.username)

    # Админ — всегда полный доступ
    if is_admin_user(user.telegram_id, user.username):
        return True

    # Если есть активная подписка — без ограничений
    if user.has_active_subscription:
        return True

    # Лимиты бесплатного режима
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
    Регистрируем использование ИИ.
    Для админа и подписчиков счетчики не увеличиваем.
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
    """
    Текст «мини-кабинета» пользователя.
    """
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


# --- ПОЛЬЗОВАТЕЛЬСКИЕ КОМАНДЫ / КНОПКИ ---


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
    """
    Экран выбора режима.
    """
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
    """
    Пользователь выбрал режим.
    """
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
    """
    Создание инвойса через Crypto Pay и сохранение его в БД.
    """
    if not message.from_user:
        return

    user = get_or_create_user(message.from_user.id, message.from_user.username)
    amount = 5.0  # 5$ в выбранной валюте

    try:
        invoice = await create_invoice(
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

    invoice_id = int(invoice["invoice_id"])
    status = str(invoice["status"])
    asset = str(invoice["currency"])
    amount_value = float(invoice["amount"])
    created_at = int(invoice["created_at"])
    url = str(invoice["url"])

    # Сохраняем платёж как active
    create_payment(
        telegram_id=user.telegram_id,
        invoice_id=invoice_id,
        currency=asset,
        amount=amount_value,
        status=status,
        created_at=created_at,
    )

    await message.answer(
        "Я создал для тебя платёжный счёт через @CryptoBot.\n\n"
        f"Нажми по ссылке ниже, чтобы оплатить <b>5 {currency}</b> за 30 дней доступа:\n"
        f"{url}\n\n"
        "После оплаты вернись в бота — доступ будет активирован автоматически в течение пары секунд.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )


# --- АДМИН-РАЗДЕЛЫ ---


@subscription_router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    """
    Главный экран админ-панели.
    """
    if not message.from_user:
        return

    if not is_admin_user(message.from_user.id, message.from_user.username):
        await message.answer("Эта команда доступна только владельцу бота.")
        return

    stats = get_stats()
    total_users = stats["total_users"]
    active_premium = stats["active_premium_users"]
    total_paid_invoices = stats["total_paid_invoices"]
    estimated_mrr = active_premium * 5  # 5$ за подписку

    text_lines = [
        "<b>Админ-панель</b>",
        "",
        f"Всего пользователей: <b>{total_users}</b>",
        f"Активных премиум-подписок: <b>{active_premium}</b>",
        f"Всего оплаченных инвойсов: <b>{total_paid_invoices}</b>",
        "",
        f"Оценочный текущий MRR: <b>{estimated_mrr}$</b> (5$ × активные премиум-подписки).",
        "",
        "<b>Разделы:</b>",
        "• /admin_premium — список активных премиум-пользователей",
        "• /admin_payments — последние платежи",
        "• /admin_export — выгрузка CSV (юзеры + платежи)",
    ]

    await message.answer("\n".join(text_lines), reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(Command("admin_premium"))
async def cmd_admin_premium(message: Message) -> None:
    """
    Список активных премиум-пользователей.
    """
    if not message.from_user:
        return

    if not is_admin_user(message.from_user.id, message.from_user.username):
        await message.answer("Эта команда доступна только владельцу бота.")
        return

    users = get_premium_users(limit=50)
    if not users:
        await message.answer("Пока нет активных премиум-подписок.")
        return

    now_ts = int(time.time())
    lines = ["<b>Активные премиум-пользователи</b>", ""]

    for u in users:
        days_left = max(0, int((u.paid_until or now_ts) - now_ts) // (24 * 60 * 60))
        paid_until_dt = datetime.fromtimestamp(u.paid_until or now_ts)
        uname = f"@{u.username}" if u.username else "(без username)"
        lines.append(
            f"• {uname} | id: <code>{u.telegram_id}</code>\n"
            f"  до: {paid_until_dt:%d.%m.%Y} (~{days_left} дн.)"
        )

    await message.answer("\n".join(lines), reply_markup=MAIN_MENU_KEYBOARD)


@subscription_router.message(Command("admin_payments"))
async def cmd_admin_payments(message: Message) -> None:
    """
    Список последних платежей.
    """
    if not message.from_user:
        return

    if not is_admin_user(message.from_user.id, message.from_user.username):
        await message.answer("Эта команда доступна только владельцу бота.")
        return

    payments = get_recent_payments(limit=30)
    if not payments:
        await message.answer("Платежей ещё не было.")
        return

    lines = ["<b>Последние платежи</b>", ""]

    for p in payments:
        user = get_user(p.telegram_id)
        uname = f"@{user.username}" if user and user.username else "(без username)"
        created_dt = datetime.fromtimestamp(p.created_at)
        paid_dt_str = (
            datetime.fromtimestamp(p.paid_at).strftime("%d.%m.%Y %H:%M")
            if p.paid_at
            else "—"
        )
        lines.append(
            f"• #{p.id} | invoice_id: <code>{p.invoice_id}</code>\n"
            f"  user: {uname} (id: <code>{p.telegram_id}</code>)\n"
            f"  {p.amount} {p.currency} | статус: <b>{p.status}</b>\n"
            f"  создан: {created_dt:%d.%m.%Y %H:%M} | оплачен: {paid_dt_str}"
        )

    await message.answer("\n".join(lines), reply_markup=MAIN_MENU_KEYBOARD)


def _split_text(text: str, max_len: int = 3800) -> List[str]:
    chunks: List[str] = []
    while text:
        chunk = text[:max_len]
        text = text[max_len:]
        chunks.append(chunk)
    return chunks


@subscription_router.message(Command("admin_export"))
async def cmd_admin_export(message: Message) -> None:
    """
    CSV-выгрузка пользователей + их платежей (текстом, чтобы скопировать в Excel/GS).
    """
    if not message.from_user:
        return

    if not is_admin_user(message.from_user.id, message.from_user.username):
        await message.answer("Эта команда доступна только владельцу бота.")
        return

    csv_text = export_users_and_payments(max_rows=500)
    chunks = _split_text(csv_text, max_len=3500)

    await message.answer(
        "Выгрузка CSV (первые 500 строк). Можно скопировать и вставить в Excel / Google Sheets:"
    )

    for chunk in chunks:
        await message.answer(f"<code>{chunk}</code>")
