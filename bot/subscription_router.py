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


# ------------ ИНИЦИАЛИЗАЦИЯ ------------

def init_subscriptions_storage() -> None:
    init_db()


# ------------ УТИЛИТЫ ------------

def _estimate_tokens_from_text(text: str | None) -> int:
    """
    Грубая оценка токенов по длине текста.
    ~1 токен ≈ 4 символа. Нам важно не точное число, а порядок.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


async def check_user_access(message: Message) -> bool:
    """
    Проверяем, может ли пользователь сейчас сделать запрос к ИИ.
    Сюда же добавляем ограничение по длине / токенам.
    """
    user_id = message.from_user.id
    info = get_usage_info(user_id)

    approx_tokens = _estimate_tokens_from_text(message.text)

    # Если есть активная подписка — пропускаем без ограничений.
    if info["has_subscription"]:
        return True

    # Проверяем лимит токенов для бесплатного режима
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
                "Твой запрос получился слишком длинным для бесплатного режима ✂️\n\n"
                f"Бесплатный лимит: <b>{info['tokens_limit']}</b> токенов.\n"
                f"Уже израсходовано: <b>{info['tokens_used']}</b>.\n\n"
                "Подключи подписку, чтобы снимать с меня длинные и глубокие ответы без ограничений."
            ),
            reply_markup=kb,
        )
        return False

    # Проверяем счётчик бесплатных запросов
    if info["remaining"] > 0:
        return True

    # Лимит бесплатных запросов исчерпан
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
            "Чтобы продолжить, подключи подписку и получай ответы без жестких ограничений по длине."
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
    Вызови эту функцию ПОСЛЕ успешного ответа ИИ пользователю.

    Она:
    - спишет 1 бесплатный запрос (если пользователь без подписки и ещё не выбил лимит),
    - добавит использование токенов (если передать input_tokens/output_tokens).
    """
    info = get_usage_info(telegram_id)

    # Подписка — бесплатные лимиты не трогаем
    if info["has_subscription"]:
        return

    # Списываем бесплатный запрос, если ещё есть
    if info["remaining"] > 0:
        register_ai_usage(telegram_id)

    # Списываем токены (бесплатный лимит)
    total_tokens = 0
    if input_tokens:
        total_tokens += input_tokens
    if output_tokens:
        total_tokens += output_tokens

    if total_tokens > 0:
        register_free_tokens_usage(telegram_id, total_tokens)


# ------------ КАБИНЕТ / ПРОФИЛЬ ------------

@router.message(Command("profile", "cabinet"))
async def cmd_profile(message: Message):
    """Мини-кабинет: статус, лимиты, история оплат."""
    user_id = message.from_user.id
    info = get_usage_info(user_id)
    user = get_user(user_id)
    payments = list_payments_for_user(user_id, limit=5)

    lines: list[str] = []

    lines.append("💻 <b>Твой мини-кабинет</b>")
    lines.append("")
    # Статус
    if info["has_subscription"] and user and user["subscription_until"]:
        lines.append("Статус: <b>Premium</b> 💎")
        lines.append(f"Активна до: <code>{user['subscription_until']}</code>")
    else:
        lines.append("Статус: <b>Free</b> ⚪️")
        lines.append("Подписка: <b>нет</b>")

    lines.append("")
    # Лимиты
    lines.append("📊 <b>Лимиты</b>")
    lines.append(
        f"Запросы: <b>{info['used']}</b> из <b>{info['limit']}</b> бесплатных"
    )
    lines.append(
        f"Токены: <b>{info['tokens_used']}</b> из <b>{info['tokens_limit']}</b> бесплатных"
    )

    lines.append("")
    # История оплат
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


# ------------ ЭКРАНЫ ПОДПИСОК (тексты в стиле «дорого-минималистично») ------------

@router.callback_query(F.data == "subs:open_plans")
async def cb_open_plans(callback: CallbackQuery):
    """Показываем список тарифов."""
    lines: list[str] = []

    lines.append("💎 <b>Premium-доступ к боту</b>")
    lines.append("")
    lines.append(
        "Без ограничений по глубине ответов, без нервов из-за лимитов. "
        "Просто задаёшь вопрос — я разбираю и отвечаю максимально развернуто."
    )

    for plan in PLANS.values():
        lines.append("")
        lines.append(f"<b>{plan.title}</b>")
        lines.append(plan.description)
        lines.append(
            f"Стоимость: <b>{plan.price_ton} TON</b> или <b>{plan.price_usdt} USDT</b>"
        )

    kb_rows = []
    for code, plan in PLANS.items():
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan.title} — оплатить в TON",
                    callback_data=f"subs:buy:{code}:TON",
                )
            ]
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan.title} — оплатить в USDT",
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
            f"Новая дата окончания: <code>{new_until}</code>\n\n"
            "Добро пожаловать в премиальный режим. Теперь можем копать глубже."
        ),
    )
