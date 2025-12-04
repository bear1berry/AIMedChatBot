from __future__ import annotations

import logging
import math
import time
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from .subscription_db import (
    init_db,
    get_or_create_user,
    increment_free_usage,
    user_has_active_subscription,
    set_subscription_month,
    get_admin_stats,
    list_active_subscriptions,
    list_recent_payments,
    User,
)
from .payments_crypto import create_invoice, get_invoice, CryptoPayError

logger = logging.getLogger(__name__)

subscription_router = Router(name="subscription")

# Настройки
ADMIN_USERNAMES = {"bear1berry"}
FREE_REQUESTS_LIMIT = 3
FREE_TOKENS_LIMIT = 8000  # условно, считаем 1 символ ~ 1 токен
SUB_PRICE_USD = 5.0
SUB_MONTHS = 1

TON_ASSET = "TON"
USDT_ASSET = "USDT"


def is_admin_username(username: Optional[str]) -> bool:
    return bool(username) and username.lstrip("@") in {u.lstrip("@") for u in ADMIN_USERNAMES}


def build_main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    buttons_row1 = [
        KeyboardButton(text="💬 Чат"),
        KeyboardButton(text="🔥 Режим"),
    ]
    buttons_row2 = [
        KeyboardButton(text="⭐ Подписка"),
        KeyboardButton(text="❓ Помощь"),
    ]
    rows = [buttons_row1, buttons_row2]
    if is_admin:
        rows.append([KeyboardButton(text="🛠 Админ-панель")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Напиши вопрос…",
    )


def _subscription_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💎 Оплатить в TON", callback_data="sub_buy_ton"
                ),
                InlineKeyboardButton(
                    text="💎 Оплатить в USDT", callback_data="sub_buy_usdt"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил", callback_data="sub_check_payment"
                )
            ],
        ]
    )


async def show_subscription_menu(message: Message) -> None:
    user = get_or_create_user(message.from_user.id, message.from_user.username)
    now = int(time.time())
    status_lines = []

    if user_has_active_subscription(user):
        left_days = (user.paid_until_ts - now) / 86400 if user.paid_until_ts else 0
        status_lines.append(
            f"✅ У тебя уже есть активная подписка.\n"
            f"Осталось примерно <b>{max(1, math.ceil(left_days))}</b> дн."
        )
    else:
        left_free = max(0, FREE_REQUESTS_LIMIT - user.free_requests)
        status_lines.append(
            "👋 У тебя есть лимит на 3 бесплатных запроса к ИИ.\n"
            f"Осталось бесплатных запросов: <b>{left_free}</b>."
        )

    text = (
        "✨ <b>AI Medicine / Alexander Bot — премиум режим</b>\n\n"
        "🔓 Подписка открывает:\n"
        "• Безлимитный доступ к ИИ (в разумных пределах)\n"
        "• Приоритетные ответы\n"
        "• Дополнительные режимы и фишки\n\n"
        f"💰 Стоимость: <b>{SUB_PRICE_USD}$</b> в TON или USDT за {SUB_MONTHS} мес.\n\n"
        + "\n".join(status_lines)
        + "\n\nПосле оплаты нажми кнопку «✅ Я оплатил»."
    )

    await message.answer(text, reply_markup=_subscription_inline_kb())


async def check_user_access(message: Message) -> bool:
    """
    Проверяем, можно ли дать пользователю доступ к ИИ.
    Если нет — показываем экран подписки и возвращаем False.
    """
    from_user = message.from_user

    # Админы всегда проходят
    if is_admin_username(from_user.username):
        get_or_create_user(from_user.id, from_user.username)  # чтобы админ тоже был в БД
        return True

    user = get_or_create_user(from_user.id, from_user.username)

    # Платная подписка
    if user_has_active_subscription(user):
        return True

    # Бесплатный лимит
    text = message.text or ""
    tokens_estimate = len(text)

    if user.free_requests < FREE_REQUESTS_LIMIT and (
        user.free_tokens + tokens_estimate <= FREE_TOKENS_LIMIT
    ):
        increment_free_usage(user.telegram_id, tokens_estimate)
        return True

    # Лимит закончился — шлем на экран подписки
    await show_subscription_menu(message)
    return False


@subscription_router.message(Command("subscription"))
@subscription_router.message(F.text == "⭐ Подписка")
async def cmd_subscription(message: Message) -> None:
    await show_subscription_menu(message)


@subscription_router.message(F.text == "🛠 Админ-панель")
@subscription_router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin_username(message.from_user.username):
        await message.answer("⛔ У тебя нет прав доступа к админ-панели.")
        return

    stats = get_admin_stats()
    text_lines = [
        "🛠 <b>Админ-панель</b>",
        "",
        f"👥 Всего пользователей: <b>{stats['total_users']}</b>",
        f"✅ Активных подписок: <b>{stats['active_subscriptions']}</b>",
        f"🧪 Пользовались бесплатно: <b>{stats['used_free']}</b>",
        f"🕳 Никогда не заходили: <b>{stats['never_used']}</b>",
        "",
        "Активные подписки (топ 10):",
    ]

    for u in list_active_subscriptions(limit=10):
        left_days = (
            (u.paid_until_ts - int(time.time())) / 86400 if u.paid_until_ts else 0
        )
        uname = f"@{u.username}" if u.username else str(u.telegram_id)
        text_lines.append(
            f"• {uname}: ещё ~{max(1, math.ceil(left_days))} дн."
        )

    text_lines.append("")
    text_lines.append("Последние платежи (топ 10):")
    for p in list_recent_payments(limit=10):
        uname = str(p.telegram_id)
        text_lines.append(
            f"• {uname}: {p.amount} {p.asset} — {p.status}"
        )

    await message.answer("\n".join(text_lines))


@subscription_router.callback_query(F.data == "sub_buy_ton")
async def callback_buy_ton(callback: CallbackQuery) -> None:
    await _process_buy(callback, TON_ASSET)


@subscription_router.callback_query(F.data == "sub_buy_usdt")
async def callback_buy_usdt(callback: CallbackQuery) -> None:
    await _process_buy(callback, USDT_ASSET)


async def _process_buy(callback: CallbackQuery, asset: str) -> None:
    user = get_or_create_user(callback.from_user.id, callback.from_user.username)
    payload = f"user:{user.telegram_id}"
    description = f"Подписка на AI бот ({SUB_MONTHS} мес.)"
    try:
        invoice = await create_invoice(
            asset=asset,
            amount=SUB_PRICE_USD,
            description=description,
            payload=payload,
        )
    except CryptoPayError as e:
        logger.exception("Failed to create CryptoPay invoice")
        await callback.message.answer(
            "⚠️ Не удалось создать ссылку на оплату. Попробуй ещё раз чуть позже.\n"
            f"Техническая ошибка: {e}"
        )
        await callback.answer()
        return

    pay_url = invoice.get("pay_url") or invoice.get("pay_url".upper()) or ""
    invoice_id = invoice.get("invoice_id") or invoice.get("invoiceId") or ""
    # Сохраняем платёж в БД
    from .subscription_db import create_payment  # локальный импорт, чтобы избежать циклов

    create_payment(
        telegram_id=user.telegram_id,
        invoice_id=str(invoice_id),
        asset=asset,
        amount=float(SUB_PRICE_USD),
        payload=payload,
        status=invoice.get("status", "active"),
    )

    text = (
        f"💳 Счёт на оплату создан.\n\n"
        f"Оплати <b>{SUB_PRICE_USD} {asset}</b> по ссылке:\n{pay_url}\n\n"
        "После оплаты вернись в чат и нажми «✅ Я оплатил»."
    )
    await callback.message.answer(text)
    await callback.answer()


@subscription_router.callback_query(F.data == "sub_check_payment")
async def callback_check_payment(callback: CallbackQuery) -> None:
    from .subscription_db import get_last_payment, mark_payment_paid  # локальный импорт

    user = get_or_create_user(callback.from_user.id, callback.from_user.username)
    payment = get_last_payment(user.telegram_id)
    if not payment:
        await callback.message.answer(
            "❌ Не нашёл твои последние счета.\nПопробуй сначала нажать кнопку оплаты."
        )
        await callback.answer()
        return

    try:
        invoice = await get_invoice(payment.invoice_id)
    except CryptoPayError as e:
        logger.exception("Failed to fetch invoice")
        await callback.message.answer(
            "⚠️ Не удалось проверить статус оплаты. Попробуй ещё раз позже.\n"
            f"Техническая ошибка: {e}"
        )
        await callback.answer()
        return

    status = invoice.get("status")
    if status == "paid":
        mark_payment_paid(payment.invoice_id)
        set_subscription_month(user.telegram_id, months=SUB_MONTHS)
        await callback.message.answer(
            "✅ Оплата подтверждена.\n"
            "Подписка активна! Можно продолжать пользоваться ИИ без ограничений (в разумных пределах)."
        )
    elif status in ("active", "pending"):
        await callback.message.answer(
            "⌛ Платёж ещё не зафиксирован.\n"
            "Подожди 10–30 секунд и нажми «✅ Я оплатил» снова."
        )
    else:
        await callback.message.answer(
            f"❌ Не удалось активировать подписку. Текущий статус счёта: <b>{status}</b>."
        )

    await callback.answer()
