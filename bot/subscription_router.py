from __future__ import annotations

import os
import time
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.formatting import as_list, as_marked_section, Bold

from .payments_crypto import create_invoice, fetch_invoices_statuses, CryptoPayError
from .subscription_db import (
    User,
    create_payment,
    get_admin_stats,
    get_or_create_user,
    get_payment_by_invoice,
    increment_free_usage,
    list_active_subscriptions,
    list_recent_payments,
    reset_free_counter,
    set_subscription_month,
    user_has_active_subscription,
)

subscription_router = Router()

FREE_MESSAGES_LIMIT = int(os.getenv("FREE_MESSAGES_LIMIT", "20"))
SUB_PRICE_TON = int(os.getenv("SUBSCRIPTION_PRICE_TON", "5"))  # цена за 1 месяц в TON-эквиваленте


def _admin_usernames() -> list[str]:
    raw = os.getenv("ADMIN_USERNAMES", "bear1berry")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def is_admin_username(username: Optional[str]) -> bool:
    if not username:
        return False
    return username.lower() in _admin_usernames()


def build_main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="⭐ Подписка")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🛠 Админ-панель")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Напиши вопрос...",
    )


def _format_user_status(user: User) -> str:
    parts = []
    if user_has_active_subscription(user):
        until_dt = time.strftime("%d.%m.%Y %H:%M", time.localtime(user.paid_until or 0))
        parts.append(f"✅ <b>Премиум активен до</b>: <code>{until_dt}</code>")
    else:
        parts.append("⚠️ <b>Подписка не активна</b>.")

    parts.append(
        f"💬 Бесплатные сообщения: <b>{user.free_messages_used}</b> из <b>{FREE_MESSAGES_LIMIT}</b>"
    )

    if is_admin_username(user.username):
        parts.append("👑 Админ-режим: включён (лимитов нет).")

    return "\n".join(parts)


async def show_subscription_menu(message: Message) -> None:
    user = get_or_create_user(message.from_user.id, message.from_user.username)
    text = as_list(
        Bold("⭐ Подписка и статус"),
        "",
        _format_user_status(user),
        "",
        "Премиум снимает лимиты и даёт приоритетные ответы.",
    ).as_html()

    buttons = [
        [
            InlineKeyboardButton(text="🚀 Оформить подписку", callback_data="sub_buy"),
        ],
        [
            InlineKeyboardButton(text="ℹ️ Как оплатить", callback_data="sub_help"),
        ],
    ]
    if is_admin_username(message.from_user.username):
        buttons.append(
            [InlineKeyboardButton(text="🛠 Админ-панель", callback_data="sub_admin")]
        )

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, reply_markup=kb)


@subscription_router.message(Command("subscription"))
async def cmd_subscription(message: Message) -> None:
    await show_subscription_menu(message)


@subscription_router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not is_admin_username(message.from_user.username):
        await message.answer("⛔ У тебя нет доступа к админ-панели.")
        return

    stats = get_admin_stats()
    active_users = list_active_subscriptions(limit=30)
    payments = list_recent_payments(limit=10)

    lines = [
        "<b>🛠 Админ-панель</b>",
        "",
        f"👥 Пользователей всего: <b>{stats['total_users']}</b>",
        f"⭐ Активных подписок: <b>{stats['active_subscriptions']}</b>",
        f"💰 Оплаченных счетов: <b>{stats['total_payments']}</b>",
        f"💸 Сумма оплат: <b>{stats['total_revenue']}</b> (в единицах CryptoPay)",
        "",
        "<b>Топ активных подписчиков:</b>",
    ]

    if active_users:
        for u in active_users:
            until = (
                time.strftime("%d.%m.%Y", time.localtime(u.paid_until or 0))
                if u.paid_until
                else "-"
            )
            lines.append(f" • @{u.username or u.telegram_id} — до {until}")
    else:
        lines.append(" — пока нет активных подписок")

    lines.append("")
    lines.append("<b>Последние платежи:</b>")
    if payments:
        for p in payments:
            status_emoji = "✅" if p.status == "paid" else "⏳"
            lines.append(
                f" • {status_emoji} @{p.username or p.telegram_id} — {p.amount} {p.currency} ({p.status})"
            )
    else:
        lines.append(" — платежей пока нет")

    await message.answer("\n".join(lines), reply_markup=build_main_menu(is_admin=True))


@subscription_router.callback_query(F.data == "sub_admin")
async def cb_sub_admin(call: CallbackQuery) -> None:
    await cmd_admin(call.message)  # type: ignore[arg-type]
    await call.answer()


@subscription_router.callback_query(F.data == "sub_help")
async def cb_sub_help(call: CallbackQuery) -> None:
    text = as_marked_section(
        Bold("Как оплатить через CryptoBot:"),
        "Нажми кнопку «Оплатить подписку» — откроется CryptoBot.",
        "Выбери удобный способ оплаты (TON, USDT и т.д.).",
        "После успешной оплаты вернись в бот и нажми «✅ Я оплатил».",
        marker="• ",
    ).as_html()

    await call.message.edit_text(text, reply_markup=call.message.reply_markup)
    await call.answer()


@subscription_router.callback_query(F.data == "sub_buy")
async def cb_sub_buy(call: CallbackQuery) -> None:
    user = get_or_create_user(call.from_user.id, call.from_user.username)

    try:
        invoice_id, invoice_url = await create_invoice(
            telegram_id=user.telegram_id,
            username=user.username,
            amount=SUB_PRICE_TON,
            description="Подписка AI Medicine / Alexander — 1 месяц",
        )
    except CryptoPayError as e:
        await call.answer("Ошибка создания счёта, попробуй позже.", show_alert=True)
        await call.message.answer(f"⚠️ Не удалось создать счёт: <code>{e}</code>")
        return

    create_payment(
        telegram_id=user.telegram_id,
        username=user.username,
        invoice_id=invoice_id,
        invoice_url=invoice_url,
        amount=SUB_PRICE_TON,
        currency="TON",
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💳 Оплатить", url=invoice_url),
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил", callback_data=f"sub_check:{invoice_id}"
                )
            ],
        ]
    )

    await call.message.edit_text(
        (
            "💳 <b>Оплата подписки</b>\n\n"
            f"• Тариф: 1 месяц — <b>{SUB_PRICE_TON}</b> TON (или эквивалент).\n"
            "• После оплаты нажми кнопку «✅ Я оплатил», чтобы активировать премиум."
        ),
        reply_markup=kb,
    )
    await call.answer()


@subscription_router.callback_query(F.data.startswith("sub_check:"))
async def cb_sub_check(call: CallbackQuery) -> None:
    invoice_id = call.data.split(":", 1)[1]
    payment = get_payment_by_invoice(invoice_id)
    if not payment:
        await call.answer("Счёт не найден. Попробуй создать новый.", show_alert=True)
        return

    try:
        statuses = await fetch_invoices_statuses([invoice_id])
    except CryptoPayError as e:
        await call.answer("Ошибка проверки оплаты.", show_alert=True)
        await call.message.answer(f"⚠️ Не удалось проверить счёт: <code>{e}</code>")
        return

    status = statuses.get(invoice_id)
    if status == "paid":
        # активируем подписку, если ещё не активировали
        set_subscription_month(payment.telegram_id, months=1)
        reset_free_counter(payment.telegram_id)
        await call.answer("Подписка активирована 🎉", show_alert=True)
        await call.message.edit_text(
            "🎉 <b>Подписка успешно активирована!</b>\n"
            "Лимиты сняты, можно пользоваться на полную.",
            reply_markup=None,
        )
    elif status in {"active", "pending"}:
        await call.answer("Платёж ещё не подтверждён. Попробуй через минуту.", show_alert=True)
    else:
        await call.answer("Счёт не оплачен или истёк. Создай новый.", show_alert=True)


async def check_user_access(message: Message) -> bool:
    """Проверка лимитов и подписки перед обращением к ИИ."""
    user = get_or_create_user(message.from_user.id, message.from_user.username)

    if is_admin_username(user.username):
        return True

    if user_has_active_subscription(user):
        return True

    if user.free_messages_used >= FREE_MESSAGES_LIMIT:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⭐ Оформить подписку", callback_data="sub_buy")]
            ]
        )
        await message.answer(
            (
                "⚠️ Лимит бесплатных сообщений исчерпан.\n\n"
                "Оформи подписку, чтобы продолжать пользоваться ботом без ограничений."
            ),
            reply_markup=kb,
        )
        return False

    new_used = increment_free_usage(user.telegram_id, user.username)
    remaining = max(FREE_MESSAGES_LIMIT - new_used, 0)
    if remaining in {3, 1}:
        await message.answer(
            f"ℹ️ Осталось бесплатных сообщений: <b>{remaining}</b> из {FREE_MESSAGES_LIMIT}.\n"
            "Дальше понадобится подписка."
        )
    return True
