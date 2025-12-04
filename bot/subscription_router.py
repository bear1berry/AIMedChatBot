# bot/subscription_router.py

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
    extend_subscription,
    get_payment,
    mark_payment_paid,
)
from .subscriptions import PLANS, get_plan
from .payments_crypto import create_invoice, get_invoice_status, CryptoPayError

router = Router(name="subscriptions")


# Инициализация БД — вызови init_subscriptions_storage() один раз при старте бота
def init_subscriptions_storage() -> None:
    init_db()


async def check_user_access(message: Message) -> bool:
    """
    Проверяем, может ли пользователь сейчас сделать запрос к ИИ.
    Возвращаем True, если можно продолжать, False — если нужно оформить подписку.
    """
    user_id = message.from_user.id
    info = get_usage_info(user_id)

    # Если есть активная подписка — всё ок.
    if info["has_subscription"]:
        return True

    # Если остались бесплатные запросы — пускаем.
    if info["remaining"] > 0:
        return True

    # Лимит бесплатных запросов исчерпан.
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Оформить подписку",
                    callback_data="subs:open_plans",
                )
            ]
        ]
    )
    await message.answer(
        (
            "Ты уже использовал свои 3 бесплатных запроса ✨\n\n"
            "Чтобы продолжить пользоваться ботом без ограничений, оформи подписку."
        ),
        reply_markup=kb,
    )
    return False


def register_successful_ai_usage(telegram_id: int) -> None:
    """
    Вызови эту функцию ПОСЛЕ успешного ответа ИИ пользователю,
    чтобы списать один бесплатный запрос (если пользователь без подписки).
    """
    info = get_usage_info(telegram_id)
    if info["has_subscription"]:
        return
    if info["remaining"] <= 0:
        return
    register_ai_usage(telegram_id)


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    """Профиль: показываем, сколько запросов осталось и до какого числа подписка."""
    user_id = message.from_user.id
    info = get_usage_info(user_id)

    text_lines = [
        "👤 Профиль",
        "",
        f"Бесплатные запросы: {info['used']} из {info['limit']}",
    ]
    from .subscription_db import get_user  # локальный импорт, чтобы избежать циклов
    user = get_user(user_id)
    if info["has_subscription"] and user and user["subscription_until"]:
        text_lines.append(f"Подписка: активна до {user['subscription_until']}")
    else:
        text_lines.append("Подписка: ❌ нет активной подписки")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Оформить / продлить подписку",
                    callback_data="subs:open_plans",
                )
            ]
        ]
    )

    await message.answer("\n".join(text_lines), reply_markup=kb)


@router.callback_query(F.data == "subs:open_plans")
async def cb_open_plans(callback: CallbackQuery):
    """Показываем список тарифов."""
    lines = ["🔥 Тарифы подписки:"]
    for plan in PLANS.values():
        lines.append(f"\n<b>{plan.title}</b>")
        lines.append(plan.description)
        lines.append(
            f"Стоимость: {plan.price_ton} TON / {plan.price_usdt} USDT"
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
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await callback.message.answer("\n".join(lines), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("subs:buy:"))
async def cb_buy_plan(callback: CallbackQuery):
    """
    Пользователь выбрал тариф и валюту оплаты.
    Создаём invoice через Crypto Pay и даём ссылку на оплату.
    """
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
                    text=f"Оплатить ({asset})",
                    url=pay_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="Проверить оплату",
                    callback_data=f"subs:check:{invoice_id}:{plan.code}",
                )
            ],
        ]
    )

    await callback.message.answer(
        (
            f"Счёт создан ✅\n\n"
            f"Тариф: <b>{plan.title}</b>\n"
            f"Сумма: {amount} {asset}\n\n"
            "Нажми кнопку «Оплатить», а после оплаты вернись в бот и нажми «Проверить оплату»."
        ),
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("subs:check:"))
async def cb_check_payment(callback: CallbackQuery):
    """
    Пользователь нажал «Проверить оплату».
    Проверяем статус инвойса через Crypto Pay и, если оплачен, активируем подписку.
    """
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

    # Проверяем, что этот платёж принадлежит текущему пользователю
    if int(payment["telegram_id"]) != callback.from_user.id:
        await callback.answer("Этот счёт принадлежит другому пользователю", show_alert=True)
        return

    # Если уже оплачено и подписка активирована ранее
    if payment["status"] == "paid":
        await callback.answer()
        await callback.message.answer(
            "Этот платёж уже подтверждён ✅\n"
            "Если подписка не отображается — напиши в поддержку.",
        )
        return

    # Проверяем статус у Crypto Pay
    status = await get_invoice_status(invoice_id)
    if status != "paid":
        await callback.answer()
        await callback.message.answer(
            f"Статус платежа: <b>{status or 'не найден'}</b>\n"
            "Если ты уже оплатил, подожди 1–2 минуты и попробуй снова.",
        )
        return

    # Помечаем платёж оплаченным и продлеваем подписку
    mark_payment_paid(invoice_id)
    new_until = extend_subscription(callback.from_user.id, plan.days)

    await callback.answer()
    await callback.message.answer(
        (
            "Оплата получена ✅\n\n"
            f"Подписка <b>{plan.title}</b> активирована.\n"
            f"Новая дата окончания: <code>{new_until}</code>"
        ),
    )
