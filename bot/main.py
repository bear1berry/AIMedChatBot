from __future__ import annotations

import asyncio
import logging
from typing import Dict, Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

from bot.config import (
    BOT_TOKEN,
    ASSISTANT_MODES,
    DEFAULT_MODE,
    OWNER_ID,
    PLAN_BASIC,
    PLAN_PREMIUM,
    DEFAULT_DAILY_LIMIT,
    REF_BONUS_PER_USER,
    SUBSCRIPTION_TARIFFS,
)
from services.storage import Storage
from services.llm import generate_answer
from services.payments import create_cryptobot_invoice, get_invoice_status


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

dp = Dispatcher()
storage = Storage()

# --- UI labels (таскбар) ---

BTN_MODES = "🧠 Режимы"
BTN_PROFILE = "👤 Профиль"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_REFERRALS = "👥 Рефералы"
BTN_BACK = "⬅️ Назад"

# Кнопки тарифов
BTN_TARIFF_MONTH = "1 месяц — 7.99 USDT"
BTN_TARIFF_QUARTER = "3 месяца — 26.99 USDT"
BTN_TARIFF_YEAR = "12 месяцев — 82.99 USDT"
BTN_TARIFF_CHECK = "Проверить оплату"


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODES)],
            [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_SUBSCRIPTION)],
            [KeyboardButton(text=BTN_REFERRALS)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши запрос для BlackBox GPT…",
    )


def modes_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=mode_cfg["button"])]
        for mode_cfg in ASSISTANT_MODES.values()
    ]
    rows.append([KeyboardButton(text=BTN_BACK)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выбери режим…",
    )


def subscription_keyboard(has_pending: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_TARIFF_MONTH)],
        [KeyboardButton(text=BTN_TARIFF_QUARTER)],
        [KeyboardButton(text=BTN_TARIFF_YEAR)],
    ]
    if has_pending:
        rows.append([KeyboardButton(text=BTN_TARIFF_CHECK)])
    rows.append([KeyboardButton(text=BTN_BACK)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выбери тариф…",
    )


# --- helpers ---


def _get_mode_config(mode: str) -> Dict[str, Any]:
    return ASSISTANT_MODES.get(mode) or ASSISTANT_MODES[DEFAULT_MODE]


def _format_plan(user: Dict[str, Any], is_owner: bool) -> str:
    if is_owner:
        return "Админ (без лимитов)"

    plan = user.get("plan") or PLAN_BASIC
    if plan == PLAN_PREMIUM and storage.is_premium_active(user):
        until = user.get("premium_until")
        return f"Premium до {until}"
    return "Базовый"


def _format_limits(user: Dict[str, Any], is_owner: bool) -> str:
    if is_owner:
        return "Лимиты: отключены для админа."
    limit = storage.get_daily_limit(user)
    used = int(user.get("daily_used") or 0)
    if limit is None:
        return f"Лимиты: Premium (без ограничений, использовано сегодня {used})."
    remain = max(limit - used, 0)
    return f"Лимиты: {used}/{limit} сегодня, осталось {remain}."


async def _send_streaming_answer(message: Message, answer: str) -> None:
    """
    Псевдо-стриминг: сначала отправляем «…», потом обновляем сообщение кусками.
    """
    # Если ответ очень длинный — просто отправляем без стриминга
    if len(answer) > 1800:
        await message.answer(answer)
        return

    msg = await message.answer("…")
    chunk_size = 250
    text = answer
    for i in range(chunk_size, len(text) + chunk_size, chunk_size):
        part = text[:i]
        try:
            await msg.edit_text(part)
        except Exception:
            # Если что-то пошло не так — просто высылаем весь текст отдельным сообщением
            await message.answer(text)
            return
        await asyncio.sleep(0.08)


# --- handlers ---


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id
    user, created = storage.get_or_create_user(user_id, message.from_user)

    # Обработка реферала, если есть deep-link
    if command.args:
        ref_code = command.args.strip()
        applied = storage.apply_referral(user_id, ref_code)
        if applied:
            await message.answer(
                "Ты зашёл по реферальной ссылке. "
                "Твоему другу начислен бонус к лимиту, а ты можешь просто пользоваться ботом 🙂"
            )

    mode_cfg = _get_mode_config(user.get("mode") or DEFAULT_MODE)
    emoji = mode_cfg["emoji"]
    title = mode_cfg["title"]

    text = f"""👋 Привет. Я **BlackBox GPT — Universal AI Assistant.**

Минималистичный интерфейс. Максимум мозга.

Снизу — таскбар:
- {BTN_MODES} — выбор режима (мозг под твою задачу)
- {BTN_PROFILE} — профиль и лимиты
- {BTN_SUBSCRIPTION} — Premium через USDT (CryptoBot)
- {BTN_REFERRALS} — реферальная система

Сейчас активен режим: *{emoji} {title}*

Просто напиши свой первый запрос 👇
"""

    await message.answer(text, reply_markup=main_keyboard(), parse_mode="Markdown")


@dp.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await message.answer("Таскбар обновлён.", reply_markup=main_keyboard())


@dp.message(F.text == BTN_BACK)
async def on_back(message: Message) -> None:
    await message.answer("Возврат к главному экрану.", reply_markup=main_keyboard())


@dp.message(F.text == BTN_MODES)
async def on_modes(message: Message) -> None:
    lines = []
    for cfg in ASSISTANT_MODES.values():
        lines.append(f"{cfg['button']} — {cfg['description']}")
    text = "Выбери режим работы ассистента:\n\n" + "\n".join(lines)
    await message.answer(text, reply_markup=modes_keyboard())


# Выбор конкретного режима
MODE_BUTTONS = {cfg["button"]: code for code, cfg in ASSISTANT_MODES.items()}


@dp.message(F.text.in_(list(MODE_BUTTONS.keys())))
async def on_mode_selected(message: Message) -> None:
    user_id = message.from_user.id
    code = MODE_BUTTONS[message.text]
    cfg = _get_mode_config(code)
    storage.set_mode(user_id, code)
    text = (
        f"Режим переключён на {cfg['button']}\n\n"
        "Теперь просто напиши запрос в этом стиле — я подстроюсь."
    )
    await message.answer(text, reply_markup=main_keyboard())


@dp.message(F.text == BTN_PROFILE)
async def on_profile(message: Message) -> None:
    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)
    is_owner = OWNER_ID is not None and OWNER_ID == user_id

    mode_cfg = _get_mode_config(user.get("mode") or DEFAULT_MODE)
    plan = _format_plan(user, is_owner)
    limits = _format_limits(user, is_owner)

    text = (
        "👤 *Профиль*\n\n"
        f"ID: `{user_id}`\n"
        f"Режим: {mode_cfg['button']}\n"
        f"План: {plan}\n"
        f"{limits}\n\n"
        f"Реферальный код: `{user.get('ref_code')}`\n"
        f"Приглашено друзей: {int(user.get('ref_count') or 0)}\n"
        f"Бонус к лимиту: +{int(user.get('ref_bonus_messages') or 0)} сообщений/день\n"
    )

    await message.answer(text, reply_markup=main_keyboard(), parse_mode="Markdown")


@dp.message(F.text == BTN_REFERRALS)
async def on_referrals(message: Message) -> None:
    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)

    ref_code = user.get("ref_code")
    if not ref_code:
        storage.get_or_create_user(user_id, message.from_user)
        user = storage.get_user(user_id)
        ref_code = user.get("ref_code")

    me = await message.bot.get_me()
    link = f"https://t.me/{me.username}?start={ref_code}"

    text = (
        "👥 *Рефералы*\n\n"
        "1. Отправь эту ссылку друзьям.\n"
        "2. Когда они запустят бота и начнут им пользоваться, "
        f"тебе будет начислено +{REF_BONUS_PER_USER} сообщений/день к базовому лимиту.\n\n"
        f"Твоя ссылка:\n{link}\n\n"
        f"Уже пришло: {int(user.get('ref_count') or 0)} друзей."
    )
    await message.answer(text, reply_markup=main_keyboard(), parse_mode="Markdown")


@dp.message(F.text == BTN_SUBSCRIPTION)
async def on_subscription(message: Message) -> None:
    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)
    is_owner = OWNER_ID is not None and OWNER_ID == user_id

    if is_owner:
        await message.answer(
            "У тебя режим админа — подписка не нужна, лимитов нет 🙂",
            reply_markup=main_keyboard(),
        )
        return

    if storage.is_premium_active(user):
        await message.answer(
            f"У тебя уже активен Premium до {user.get('premium_until')}.\n"
            "Когда срок закончится, можно будет оформить продление.",
            reply_markup=main_keyboard(),
        )
        return

    has_pending = bool(user.get("pending_invoice_id"))
    text = (
        "💎 *Подписка Premium*\n\n"
        "Premium снимает лимиты и даёт приоритетные ответы.\n\n"
        "Тарифы (оплата через CryptoBot, USDT):\n"
        f"- {BTN_TARIFF_MONTH}\n"
        f"- {BTN_TARIFF_QUARTER}\n"
        f"- {BTN_TARIFF_YEAR}\n\n"
        "После выбора тарифа ты получишь ссылку на оплату в CryptoBot."
    )
    if has_pending:
        text += "\n\nУ тебя есть неоплаченный счёт — можно нажать «Проверить оплату»."
    await message.answer(text, reply_markup=subscription_keyboard(has_pending), parse_mode="Markdown")


TARIFF_BY_BUTTON = {
    BTN_TARIFF_MONTH: "month",
    BTN_TARIFF_QUARTER: "quarter",
    BTN_TARIFF_YEAR: "year",
}


@dp.message(F.text.in_(list(TARIFF_BY_BUTTON.keys())))
async def on_tariff_selected(message: Message) -> None:
    user_id = message.from_user.id
    tariff_code = TARIFF_BY_BUTTON[message.text]
    tariff = SUBSCRIPTION_TARIFFS[tariff_code]

    await message.answer("Генерирую ссылку на оплату через CryptoBot…")

    try:
        created = await create_cryptobot_invoice(tariff_code, user_id)
    except Exception:
        logger.exception("create_cryptobot_invoice error")
        await message.answer("Не удалось создать счёт. Попробуй чуть позже.")
        return

    if not created:
        await message.answer("Оплата временно недоступна. Попробуй позже.")
        return

    invoice_id, url = created
    storage.set_pending_invoice(user_id, invoice_id, tariff_code)

    text = (
        f"Счёт на *{tariff['title']}* успешно создан.\n\n"
        f"Ссылка на оплату в CryptoBot:\n{url}\n\n"
        "После оплаты вернись в чат с ботом и нажми «Проверить оплату»."
    )
    user = storage.get_user(user_id)
    has_pending = bool(user and user.get("pending_invoice_id"))
    await message.answer(text, reply_markup=subscription_keyboard(has_pending), parse_mode="Markdown")


@dp.message(F.text == BTN_TARIFF_CHECK)
async def on_tariff_check(message: Message) -> None:
    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)
    pending_id = user.get("pending_invoice_id")
    tariff_code = user.get("pending_invoice_tariff")

    if not pending_id or not tariff_code:
        await message.answer(
            "Нет активного счёта. Выбери тариф, чтобы создать новый.",
            reply_markup=subscription_keyboard(False),
        )
        return

    await message.answer("Проверяю оплату…")

    try:
        status = await get_invoice_status(int(pending_id))
    except Exception:
        logger.exception("get_invoice_status error")
        await message.answer("Не удалось проверить статус оплаты. Попробуй позже.")
        return

    if status == "paid":
        storage.activate_premium(user_id, tariff_code)
        user = storage.get_user(user_id)
        await message.answer(
            f"Оплата подтверждена ✅\n\n"
            f"Premium активирован до {user.get('premium_until')}.",
            reply_markup=main_keyboard(),
        )
    elif status == "active":
        await message.answer(
            "Счёт ещё не оплачен. После оплаты нажми «Проверить оплату» ещё раз.",
            reply_markup=subscription_keyboard(True),
        )
    elif status == "expired":
        storage.clear_pending_invoice(user_id)
        await message.answer(
            "Счёт истёк. Создай новый, выбрав тариф ещё раз.",
            reply_markup=subscription_keyboard(False),
        )
    else:
        await message.answer(
            f"Не удалось определить статус оплаты (status={status!r}). "
            "Попробуй позже или создай новый счёт.",
            reply_markup=subscription_keyboard(True),
        )


# --- обработка обычных запросов ---


@dp.message(F.text)
async def on_user_message(message: Message) -> None:
    text = message.text
    # Игнорим служебные кнопки (на случай, если что-то не отловилось отдельными хендлерами)
    if text in {
        BTN_MODES,
        BTN_PROFILE,
        BTN_SUBSCRIPTION,
        BTN_REFERRALS,
        BTN_BACK,
        BTN_TARIFF_MONTH,
        BTN_TARIFF_QUARTER,
        BTN_TARIFF_YEAR,
        BTN_TARIFF_CHECK,
    }:
        return

    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)
    is_owner = OWNER_ID is not None and OWNER_ID == user_id

    # Лимиты (не действуют для админа)
    if not is_owner:
        limit = storage.get_daily_limit(user)
        used = int(user.get("daily_used") or 0)
        if limit is not None and used >= limit:
            await message.answer(
                "На сегодня лимит сообщений исчерпан.\n\n"
                "Варианты:\n"
                "• Подождать до завтра — лимит обновится автоматически.\n"
                "• Оформить Premium в разделе «Подписка» для снятия ограничений."
            )
            return

    mode = user.get("mode") or DEFAULT_MODE
    mode_cfg = _get_mode_config(mode)

    # История диалога в рамках режима
    history = storage.get_history(user, mode)

    # Показываем индикатор печати
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass

    # Запрос к LLM
    try:
        answer = await generate_answer(
            system_prompt=mode_cfg["system_prompt"],
            history=history,
            user_message=text,
        )
    except Exception:
        logger.exception("LLM error")
        await message.answer("Что-то пошло не так при обращении к модели. Попробуй ещё раз чуть позже.")
        return

    # Обновляем лимиты и историю
    if not is_owner:
        user = storage.register_usage(user, count=1)
    storage.add_history_message(user, mode, "user", text)
    storage.add_history_message(user, mode, "assistant", answer)

    await _send_streaming_answer(message, answer)


# --- entrypoint ---


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    me = await bot.get_me()
    logger.info("Starting bot as @%s (id=%s)", me.username, me.id)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
