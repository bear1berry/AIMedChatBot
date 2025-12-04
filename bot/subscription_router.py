# bot/subscription_router.py

import os
from typing import List

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from .subscription_db import (
    init_db,
    get_usage_info,
    register_ai_usage,
    register_free_tokens_usage,
    extend_subscription,
    get_payment,
    mark_payment_paid,
    list_payments_for_user,
    can_consume_free_tokens,
    get_user,
)
from .subscriptions import PLANS, get_plan
from .payments_crypto import create_invoice, get_invoice_status, CryptoPayError

router = Router(name="subscriptions")

CRYPTO_STATIC_INVOICE_URL = os.getenv("CRYPTO_STATIC_INVOICE_URL")


# -------- инициализация --------

def init_subscriptions_storage() -> None:
    init_db()


# -------- утилиты --------

def _estimate_tokens_from_text(text: str | None) -> int:
    """Грубая оценка токенов по длине текста (~1 токен ≈ 4 символа)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


async def check_user_access(message: Message) -> bool:
    """
    Проверяем, может ли пользователь сделать запрос к ИИ.
    Учитываем:
    - наличие подписки,
    - лимит бесплатных запросов,
    - лимит бесплатных токенов.
    """
    user_id = message.from_user.id
    info = get_usage_info(user_id)
    approx_tokens = _estimate_tokens_from_text(message.text)

    # Подписка — пропускаем всё.
    if info["has_subscription"]:
        return True

    # Проверка токенов
    if not can_consume_free_tokens(user_id, approx_tokens):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="💎 Оформить подписку",
                        callback_data="subs:open_plans",
                    )
                ]
            ]
        )
        await message.answer(
            (
                "Твой запрос получился слишком объёмным для бесплатного режима ✂️\n\n"
                f"Бесплатный лимит: <b>{info['tokens_limit']}</b> токенов.\n"
                f"Уже израсходовано: <b>{info['tokens_used']}</b>.\n\n"
                "Подключи подписку, чтобы получать длинные и глубокие ответы без ограничений."
            ),
            reply_markup=kb,
        )
        return False

    # Проверка количества запросов
    if info["remaining"] > 0:
        return True

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💎 Оформить подписку",
                    callback_data="subs:open_plans",
                )
            ]
        ]
    )
    await message.answer(
        (
            "Ты уже использовал свои 3 бесплатных запроса ✨\n\n"
            "Подключи подписку — и продолжим работу в премиальном режиме без жёстких ограничений."
        ),
        reply_markup=kb,
    )
    return False


def register_successful_ai_usage(
    telegram_id: int,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """
    Вызывается ПОСЛЕ успешного ответа ИИ.

    - списывает бесплатный запрос (если он ещё есть и нет подписки),
    - списывает бесплатные токены.
    """
    info = get_usage_info(telegram_id)

    if info["has_subscription"]:
        return

    if info["remaining"] > 0:
        register_ai_usage(telegram_id)

    total_tokens = 0
    if input_tokens:
        total_tokens += input_tokens
    if output_tokens:
        total_tokens += output_tokens

    if total_tokens > 0:
        register_free_tokens_usage(telegram_id, total_tokens)


# -------- мини-кабинет / профиль --------

@router.message(Command("profile", "cabinet"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    info = get_usage_info(user_id)
    user = get_user(user_id)
    payments = list_payments_for_user(user_id, limit=5)

    lines: List[str] = []
    lines.append("💻 <b>Твой мини-кабинет</b>")
    lines.append("")

    if info["has_subscription"] and user and user["subscription_until"]:
        lines.append("Статус: <b>Premium</b> 💎")
        lines.append(f"Активна до: <code>{user['subscription_until']}</code>")
    else:
        lines.append("Статус: <b>Free</b> ⚪️")
        lines.append("Подписка: <b>нет</b>")

    lines.append("")
    lines.append("📊 <b>Лимиты</b>")
    lines.append(
        f"Запросы: <b>{info['used']}</b> из <b>{info['limit']}</b> бесплатных"
    )
    lines.append(
        f"Токены: <b>{info['tokens_used']}</b> из <b>{info['tokens_limit']}</b> бесплатных"
    )

    lines.append("")
    lines.append("💳 <b>История оплат</b> (последние 5):")
    if not payments:
        lines.append("Пока нет ни одного платежа.")
    else:
        for p in payments:
            status = p["status"]
            if status == "paid":
                status_emoji = "✅"
            elif status == "pending":
                status_emoji = "⏳"
            else:
                status_emoji = "⚠️"

            created = p["created_at"]
            plan_code = p["plan_code"]
            asset = p["asset"]
            amount = p["amount"]

            lines.append(
                f"{status_emoji} {created} — {amount} {asset} — тариф <code>{plan_code}</code> ({status})"
            )

    lines.append("")
    lines.append("ℹ️ Команды: /profile — кабинет, /faq — ответы на вопросы.")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💎 Оформить / продлить подписку",
                    callback_data="subs:open_plans",
                )
            ]
        ]
    )

    await message.answer("\n".join(lines), reply_markup=kb)


# -------- FAQ --------

@router.message(Command("faq"))
async def cmd_faq(message: Message):
    text = (
        "❓ <b>FAQ по подписке</b>\n\n"
        "<b>Как оплатить?</b>\n"
        "— Нажми «Оформить подписку» или команду /profile.\n"
        "— Выбери оплату в TON или USDT.\n"
        "— Бот откроет окно оплаты через CryptoBot в Telegram.\n"
        "— После перевода вернись в бота и нажми «Проверить оплату».\n\n"
        "<b>Куда попадают деньги?</b>\n"
        "— Все средства зачисляются на мой криптокошелёк в Telegram (CryptoBot/@wallet), "
        "привязанный к этому боту. Оттуда я могу вывести их на биржу или внешний кошелёк.\n\n"
        "<b>Есть ли автосписания?</b>\n"
        "— Нет. Автосписаний нет, подписка не продлевается автоматически. "
        "Когда срок закончится — доступ просто вернётся в бесплатный режим.\n\n"
        "<b>Как отменить подписку?</b>\n"
        "— Ничего отменять не нужно. Просто не оплачивай следующий счёт. "
        "Если оплатил по ошибке — напиши в поддержку, разберёмся."
    )
    await message.answer(text)


# -------- экраны подписки --------

@router.callback_query(F.data == "subs:open_plans")
async def cb_open_plans(callback: CallbackQuery):
    lines: List[str] = []

    lines.append("💎 <b>Premium-доступ</b>")
    lines.append("")
    lines.append(
        "Режим без жестких лимитов по длине и глубине ответов.\n"
        "Ты задаёшь вопрос — я разбираю ситуацию до основания и выдаю максимум пользы."
    )

    for plan in PLANS.values():
        lines.append("")
        lines.append(f"<b>{plan.title}</b>")
        lines.append(plan.description)
        lines.append(
            f"Стоимость: <b>{plan.price_ton} TON</b> или <b>{plan.price_usdt} USDT</b> в месяц."
        )

    kb_rows = []
    for code, plan in PLANS.items():
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan.title} — TON",
                    callback_data=f"subs:buy:{code}:TON",
                )
            ]
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan.title} — USDT",
                    callback_data=f"subs:buy:{code}:USDT",
                )
            ]
        )

    if CRYPTO_STATIC_INVOICE_URL:
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="Оплатить напрямую (TON/USDT)",
                    url=CRYPTO_STATIC_INVOICE_URL,
                )
            ]
        )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await callback.message.answer("\n".join(lines), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("subs:buy:"))
async def cb_buy_plan(callback: CallbackQuery):
    user_id = callback.from_user.id
    _, _, plan_code, asset = callback.data.split(":", 3)
    plan = get_plan(plan_code)
    if not plan:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    amount = plan.price_ton if asset == "TON" else plan.price_usdt

    try:
        invoice = await create_invoice(
            telegram_id=user_id,
            plan_code=plan.code,
            asset=asset,  # type: ignore[arg-type]
            amount=amount,
            description=f"{plan.title} ({asset})",
        )
    except CryptoPayError:
        await callback.answer()
        await callback.message.answer(
            "Не удалось создать счёт на оплату. Попробуй чуть позже 🙏"
        )
        return

    pay_url = invoice["pay_url"]
    invoice_id = invoice["invoice_id"]

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💸 Оплатить ({asset})",
                    url=pay_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔁 Проверить оплату",
                    callback_data=f"subs:check:{invoice_id}:{plan.code}",
                )
            ],
        ]
    )

    await callback.message.answer(
        (
            f"Счёт создан ✅\n\n"
            f"Тариф: <b>{plan.title}</b>\n"
            f"Сумма: <b>{amount} {asset}</b>\n\n"
            "1) Нажми «Оплатить» и заверши перевод в Telegram-кошельке.\n"
            "2) Вернись в этого бота и нажми «Проверить оплату»."
        ),
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("subs:check:"))
async def cb_check_payment(callback: CallbackQuery):
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    _, _, invoice_id, plan_code = parts
    plan = get_plan(plan_code)
    if not plan:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    payment = get_payment(invoice_id)
    if not payment:
        await callback.answer("Счёт не найден", show_alert=True)
        return

    if int(payment["telegram_id"]) != callback.from_user.id:
        await callback.answer("Этот счёт принадлежит другому пользователю", show_alert=True)
        return

    if payment["status"] == "paid":
        await callback.answer()
        await callback.message.answer(
            "Этот платёж уже подтверждён ✅\n"
            "Если подписка не отображается — напиши в поддержку.",
        )
        return

    status = await get_invoice_status(invoice_id)
    if status != "paid":
        await callback.answer()
        await callback.message.answer(
            f"Статус платежа: <b>{status or 'не найден'}</b>\n"
            "Если ты уже оплатил, подожди 1–2 минуты и попробуй снова.",
        )
        return

    mark_payment_paid(invoice_id)
    new_until = extend_subscription(callback.from_user.id, plan.days)

    await callback.answer()
    await callback.message.answer(
        (
            "Оплата получена ✅\n\n"
            f"Подписка <b>{plan.title}</b> активирована.\n"
            f"Новая дата окончания: <code>{new_until}</code>\n\n"
            "Добро пожаловать в премиальный режим. Теперь можно копать глубже."
        ),
    )
