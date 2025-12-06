from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

from bot.config import (
    BOT_TOKEN,
    ASSISTANT_MODES,
    DEFAULT_MODE_KEY,
    FREE_DAILY_LIMIT,
    FREE_MONTHLY_LIMIT,
    PREMIUM_DAILY_LIMIT,
    PREMIUM_MONTHLY_LIMIT,
    MAX_INPUT_TOKENS,
    SUBSCRIPTION_TARIFFS,
    REF_BASE_URL,
)
from bot import text as txt
from services.llm import ask_llm_stream
from services.storage import Storage, UserRecord
from services.payments import create_cryptobot_invoice, get_invoice_status

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# --- UI LABELS ---

BTN_MODES = "🧠 Режимы"
BTN_PROFILE = "👤 Профиль"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_REFERRALS = "👥 Рефералы"

BTN_MODE_UNIVERSAL = "🧠 Универсальный"
BTN_MODE_MEDICINE = "🩺 Медицина"
BTN_MODE_COACH = "🔥 Наставник"
BTN_MODE_BUSINESS = "💼 Бизнес"
BTN_MODE_CREATIVE = "🎨 Креатив"

BTN_BACK_MAIN = "⬅️ Назад"

BTN_SUB_1M = "💎 Premium · 1 месяц"
BTN_SUB_3M = "💎 Premium · 3 месяца"
BTN_SUB_12M = "💎 Premium · 12 месяцев"
BTN_SUB_CHECK = "🔄 Проверить оплату"

# --- Keyboards ---

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MODES), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_SUBSCRIPTION), KeyboardButton(text=BTN_REFERRALS)],
    ],
    resize_keyboard=True,
)

MODES_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MODE_UNIVERSAL), KeyboardButton(text=BTN_MODE_MEDICINE)],
        [KeyboardButton(text=BTN_MODE_COACH), KeyboardButton(text=BTN_MODE_BUSINESS)],
        [KeyboardButton(text=BTN_MODE_CREATIVE)],
        [KeyboardButton(text=BTN_BACK_MAIN)],
    ],
    resize_keyboard=True,
)

SUB_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_SUB_1M)],
        [KeyboardButton(text=BTN_SUB_3M)],
        [KeyboardButton(text=BTN_SUB_12M)],
        [KeyboardButton(text=BTN_SUB_CHECK)],
        [KeyboardButton(text=BTN_BACK_MAIN)],
    ],
    resize_keyboard=True,
)

REF_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_BACK_MAIN)],
    ],
    resize_keyboard=True,
)

# --- Core objects ---

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.MARKDOWN)
dp = Dispatcher()
router = Router()
storage = Storage()


# --- helpers ---

def _plan_title(plan_code: str, is_admin: bool) -> str:
    if is_admin or plan_code == "admin":
        return "Admin"
    if plan_code == "premium":
        return "Premium"
    return "Базовый"


def _mode_title(mode_key: str) -> str:
    mode = ASSISTANT_MODES.get(mode_key) or ASSISTANT_MODES[DEFAULT_MODE_KEY]
    return mode["title"]


def _estimate_prompt_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _check_limits(user: UserRecord, plan_code: str, is_admin: bool) -> Optional[str]:
    if is_admin or plan_code == "admin":
        return None

    if plan_code == "premium":
        daily_max = PREMIUM_DAILY_LIMIT
        monthly_max = PREMIUM_MONTHLY_LIMIT
    else:
        daily_max = FREE_DAILY_LIMIT
        monthly_max = FREE_MONTHLY_LIMIT

    if user.daily_used >= daily_max:
        return "Достигнут дневной лимит запросов для текущего тарифа."
    if user.monthly_used >= monthly_max:
        return "Достигнут месячный лимит запросов для текущего тарифа."
    return None


async def _send_streaming_answer(message: Message, user: UserRecord, text: str) -> None:
    typing = await message.answer("⌛ Думаю...", reply_markup=MAIN_KB)

    style_hint = user.style_hint or ""
    try:
        last_chunk = None
        async for chunk in ask_llm_stream(
            mode_key=user.mode_key or DEFAULT_MODE_KEY,
            user_prompt=text,
            style_hint=style_hint,
        ):
            last_chunk = chunk
            full = chunk["full"]
            # ограничиваем до лимита Telegram
            if len(full) > 4000:
                full = full[:3990] + "…"
            try:
                await typing.edit_text(full)
            except Exception as e:
                logger.debug("Failed to edit message while streaming: %s", e)
                break
        # Обновляем статистику по токенам
        tokens = last_chunk.get("tokens", 0) if last_chunk else 0
        storage.apply_usage(user, tokens)
    except Exception as e:
        logger.exception("LLM error: %s", e)
        await typing.edit_text(txt.render_generic_error())


def _tariff_key_by_button(button_text: str) -> Optional[str]:
    if button_text == BTN_SUB_1M:
        return "month_1"
    if button_text == BTN_SUB_3M:
        return "month_3"
    if button_text == BTN_SUB_12M:
        return "month_12"
    return None


# --- Handlers ---

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    full_text = message.text or ""
    parts = full_text.split(maxsplit=1)
    start_param = parts[1].strip() if len(parts) > 1 else ""

    user, created = storage.get_or_create_user(user_id, message.from_user)

    # Рефералы
    if start_param.startswith("ref_") and created:
        ref_code = start_param.replace("ref_", "", 1)
        storage.apply_referral(user_id, ref_code)
        user, _ = storage.get_or_create_user(user_id, message.from_user)

    is_admin = storage.is_admin(user_id)
    plan_code = storage.effective_plan(user, is_admin)
    plan_title = _plan_title(plan_code, is_admin)
    mode_title = _mode_title(user.mode_key)

    text_body = txt.render_onboarding(
        first_name=message.from_user.first_name,
        is_new=created,
        plan_title=plan_title,
        mode_title=mode_title,
    )
    await message.answer(text_body, reply_markup=MAIN_KB)
    logger.info("User %s started bot (created=%s, plan=%s, mode=%s)", user_id, created, plan_code, user.mode_key)


@router.message(F.text == BTN_BACK_MAIN)
async def on_back_main(message: Message) -> None:
    await message.answer("Возвращаю на главный экран.", reply_markup=MAIN_KB)


@router.message(F.text == BTN_PROFILE)
async def on_profile(message: Message) -> None:
    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)
    is_admin = storage.is_admin(user_id)
    plan_code = storage.effective_plan(user, is_admin)
    plan_title = _plan_title(plan_code, is_admin)

    text_body = txt.render_profile(
        plan_code=plan_code,
        plan_title=plan_title,
        is_admin=is_admin,
        daily_used=user.daily_used,
        monthly_used=user.monthly_used,
        premium_until=user.premium_until,
        total_requests=user.total_requests,
        total_tokens=user.total_tokens,
        ref_code=user.ref_code,
    )
    await message.answer(text_body, reply_markup=MAIN_KB)


@router.message(F.text == BTN_MODES)
async def on_modes_root(message: Message) -> None:
    text_body = (
        "🧠 *Режимы работы*\n\n"
        "Выбери, через какую «линзу» я буду смотреть на твой запрос:\n"
        "• *Универсальный* — по умолчанию, для всего.\n"
        "• *Медицина* — аккуратный медицинский ассистент.\n"
        "• *Наставник* — фокус на рост, систему и дисциплину.\n"
        "• *Бизнес* — стратегии, решения, деньги.\n"
        "• *Креатив* — идеи, тексты, концепции.\n\n"
        "Режим влияет на акценты и подачу, но не срезает интеллект."
    )
    await message.answer(text_body, reply_markup=MODES_KB)


@router.message(F.text.in_({BTN_MODE_UNIVERSAL, BTN_MODE_MEDICINE, BTN_MODE_COACH, BTN_MODE_BUSINESS, BTN_MODE_CREATIVE}))
async def on_mode_select(message: Message) -> None:
    user_id = message.from_user.id
    mapping = {
        BTN_MODE_UNIVERSAL: "universal",
        BTN_MODE_MEDICINE: "medicine",
        BTN_MODE_COACH: "coach",
        BTN_MODE_BUSINESS: "business",
        BTN_MODE_CREATIVE: "creative",
    }
    mode_key = mapping.get(message.text, DEFAULT_MODE_KEY)
    storage.set_mode(user_id, mode_key)
    mode_title = _mode_title(mode_key)
    await message.answer(txt.render_mode_switched(mode_title), reply_markup=MAIN_KB)


@router.message(F.text == BTN_SUBSCRIPTION)
async def on_subscription(message: Message) -> None:
    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)
    is_admin = storage.is_admin(user_id)
    plan_code = storage.effective_plan(user, is_admin)
    plan_title = _plan_title(plan_code, is_admin)
    text_body = txt.render_subscription_overview(plan_title, user.premium_until)
    await message.answer(text_body, reply_markup=SUB_KB)


@router.message(F.text.in_({BTN_SUB_1M, BTN_SUB_3M, BTN_SUB_12M}))
async def on_subscription_buy(message: Message) -> None:
    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)

    tariff_key = _tariff_key_by_button(message.text)
    if not tariff_key:
        return

    tariff = SUBSCRIPTION_TARIFFS.get(tariff_key)
    if not tariff:
        await message.answer(txt.render_payment_error(), reply_markup=SUB_KB)
        return

    invoice = await create_cryptobot_invoice(tariff_key)
    if not invoice:
        await message.answer(txt.render_payment_error(), reply_markup=SUB_KB)
        return

    invoice_id = invoice["invoice_id"]
    invoice_url = invoice["bot_invoice_url"]

    storage.store_invoice(user, invoice_id=invoice_id, tariff_key=tariff_key)

    text_body = txt.render_payment_link(
        tariff_title=tariff["title"],
        amount=tariff["price_usdt"],
        invoice_url=invoice_url,
    )
    await message.answer(text_body, reply_markup=SUB_KB)


@router.message(F.text == BTN_SUB_CHECK)
async def on_subscription_check(message: Message) -> None:
    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)

    invoice_id, tariff_key = storage.get_last_invoice(user)
    if not invoice_id or not tariff_key:
        await message.answer(txt.render_payment_check_result("not_found"), reply_markup=SUB_KB)
        return

    status = await get_invoice_status(invoice_id)
    if not status:
        await message.answer(txt.render_payment_check_result("not_found"), reply_markup=SUB_KB)
        return

    if status == "paid":
        tariff = SUBSCRIPTION_TARIFFS.get(tariff_key)
        months = int(tariff.get("months", 1)) if tariff else 1
        storage.activate_premium(user, months)
    text_body = txt.render_payment_check_result(status)
    await message.answer(text_body, reply_markup=SUB_KB)


@router.message(F.text == BTN_REFERRALS)
async def on_referrals(message: Message) -> None:
    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)
    ref_link = f"{REF_BASE_URL}?start=ref_{user.ref_code}"
    text_body = txt.render_referrals(ref_link=ref_link, total_refs=user.referrals_count)
    await message.answer(text_body, reply_markup=REF_KB)


@router.message(F.text.startswith("/"))
async def on_unknown_command(message: Message) -> None:
    # Для любых других команд
    await message.answer(
        "Команда не распознана.\n\nИспользуй нижние кнопки навигации или просто напиши запрос.",
        reply_markup=MAIN_KB,
    )


@router.message(F.text.len() > 0)
async def on_user_message(message: Message) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer(txt.render_empty_prompt_error(), reply_markup=MAIN_KB)
        return

    if len(text) > MAX_INPUT_TOKENS * 4:
        await message.answer(txt.render_too_long_prompt_error(), reply_markup=MAIN_KB)
        return

    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)
    is_admin = storage.is_admin(user_id)
    plan_code = storage.effective_plan(user, is_admin)

    reason = _check_limits(user, plan_code, is_admin)
    if reason:
        await message.answer(txt.render_limits_warning(reason), reply_markup=MAIN_KB)
        return

    await _send_streaming_answer(message, user, text)


# --- entrypoint ---

async def main() -> None:
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
