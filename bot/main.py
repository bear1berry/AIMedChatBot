import asyncio
import logging
from datetime import datetime
from typing import Dict, Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)

from bot.config import (
    BOT_TOKEN,
    ASSISTANT_MODES,
    DEFAULT_MODE_KEY,
    ADMIN_IDS,
    LOG_CHAT_ID,
    REF_BASE_URL,
    SUBSCRIPTION_TARIFFS,
    MAX_INPUT_TOKENS,
)

from services.llm import ask_llm_stream
from services.storage import get_storage
from services.payments import create_cryptobot_invoice

from bot import text as txt


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

storage = get_storage()
router = Router()

# --- UI labels --------------------------------------------------------

BTN_MODES = "🧠 Режимы"
BTN_PROFILE = "👤 Профиль"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_REFERRALS = "👥 Рефералы"
BTN_BACK = "⬅️ Назад"

BTN_MODE_UNIVERSAL = "🧠 Универсальный"
BTN_MODE_MEDICINE = "🩺 Медицина"
BTN_MODE_COACH = "🔥 Наставник"
BTN_MODE_BUSINESS = "💼 Бизнес"
BTN_MODE_CREATIVE = "🎨 Креатив"

BTN_SUB_MONTH_1 = "💎 1 месяц"
BTN_SUB_MONTH_3 = "💎 3 месяца"
BTN_SUB_MONTH_12 = "💎 12 месяцев"

MAIN_MENU_BUTTONS = {BTN_MODES, BTN_PROFILE, BTN_SUBSCRIPTION, BTN_REFERRALS}
MODE_BUTTONS = {
    BTN_MODE_UNIVERSAL: "universal",
    BTN_MODE_MEDICINE: "medicine",
    BTN_MODE_COACH: "coach",
    BTN_MODE_BUSINESS: "business",
    BTN_MODE_CREATIVE: "creative",
}
SUB_BUTTONS = {
    BTN_SUB_MONTH_1: "month_1",
    BTN_SUB_MONTH_3: "month_3",
    BTN_SUB_MONTH_12: "month_12",
}


def build_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODES)],
            [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_SUBSCRIPTION)],
            [KeyboardButton(text=BTN_REFERRALS)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Спроси обо всём, что угодно…",
    )


def build_modes_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODE_UNIVERSAL)],
            [KeyboardButton(text=BTN_MODE_MEDICINE)],
            [KeyboardButton(text=BTN_MODE_COACH)],
            [KeyboardButton(text=BTN_MODE_BUSINESS)],
            [KeyboardButton(text=BTN_MODE_CREATIVE)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери режим работы ассистента…",
    )


def build_subscription_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SUB_MONTH_1)],
            [KeyboardButton(text=BTN_SUB_MONTH_3)],
            [KeyboardButton(text=BTN_SUB_MONTH_12)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери срок подписки…",
    )


def build_main_menu_answer_kwargs() -> Dict[str, Any]:
    """
    Набор reply_kwargs для сообщений, где по итогу пользователь
    должен оказаться в главном меню.
    """
    return {"reply_markup": build_main_keyboard()}


# --- Helpers ----------------------------------------------------------


def _get_mode_cfg(mode_key: str) -> Dict[str, Any]:
    return ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES[DEFAULT_MODE_KEY])


async def _ensure_user(message: Message) -> Dict[str, Any]:
    user_id = message.from_user.id
    tg_user = message.from_user
    # В новом Storage сигнатура get_or_create_user(user_id, tg_user=None)
    user, created = storage.get_or_create_user(user_id, tg_user=tg_user)
    if created:
        logger.info("New user %s (@%s)", user_id, tg_user.username)
    return user


# --- Handlers: /start, help, generic ---------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = await _ensure_user(message)
    user_id = message.from_user.id

    mode_key = storage.get_mode(user_id) or DEFAULT_MODE_KEY
    mode_cfg = _get_mode_cfg(mode_key)
    limits = storage.get_limits(user_id)
    ref_stats = storage.get_referral_stats(user_id)

    text_to_send = txt.render_onboarding(
        first_name=message.from_user.first_name,
        is_new=user.get("is_new", False),
        mode_title=mode_cfg["title"],
        limits=limits,
        ref_stats=ref_stats,
    )

    await message.answer(
        text_to_send,
        parse_mode=ParseMode.HTML,
        reply_markup=build_main_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await _ensure_user(message)
    await message.answer(
        txt.render_help(),
        parse_mode=ParseMode.HTML,
        **build_main_menu_answer_kwargs(),
    )


# --- Профиль ----------------------------------------------------------


@router.message(F.text == BTN_PROFILE)
@router.message(Command("profile"))
async def handle_profile(message: Message):
    user = await _ensure_user(message)
    user_id = message.from_user.id

    mode_key = storage.get_mode(user_id) or DEFAULT_MODE_KEY
    mode_cfg = _get_mode_cfg(mode_key)
    limits = storage.get_limits(user_id)
    plan = storage.get_plan(user_id)
    ref_stats = storage.get_referral_stats(user_id)

    referral_link = f"{REF_BASE_URL}?start={user['ref_code']}"

    text_to_send = txt.render_profile(
        user_id=user_id,
        tg_user=message.from_user,
        mode_cfg=mode_cfg,
        limits=limits,
        plan=plan,
        ref_stats=ref_stats,
        referral_link=referral_link,
    )

    await message.answer(
        text_to_send,
        parse_mode=ParseMode.HTML,
        **build_main_menu_answer_kwargs(),
    )


# --- Лимиты -----------------------------------------------------------


@router.message(Command("limits"))
async def handle_limits_cmd(message: Message):
    await show_limits(message)


@router.message(F.text.lower() == "лимиты")
async def handle_limits_btn(message: Message):
    await show_limits(message)


async def show_limits(message: Message):
    await _ensure_user(message)
    user_id = message.from_user.id

    mode_key = storage.get_mode(user_id) or DEFAULT_MODE_KEY
    mode_cfg = _get_mode_cfg(mode_key)
    limits = storage.get_limits(user_id)
    plan = storage.get_plan(user_id)

    text_to_send = txt.render_limits(
        mode_cfg=mode_cfg,
        limits=limits,
        plan=plan,
    )

    await message.answer(
        text_to_send,
        parse_mode=ParseMode.HTML,
        **build_main_menu_answer_kwargs(),
    )


# --- Режимы -----------------------------------------------------------


@router.message(F.text == BTN_MODES)
async def handle_modes_root(message: Message):
    await _ensure_user(message)
    await message.answer(
        txt.render_modes_root(),
        parse_mode=ParseMode.HTML,
        reply_markup=build_modes_keyboard(),
    )


@router.message(F.text.in_(list(MODE_BUTTONS.keys())))
async def handle_mode_select(message: Message):
    await _ensure_user(message)
    user_id = message.from_user.id

    btn_text = message.text
    mode_key = MODE_BUTTONS.get(btn_text, DEFAULT_MODE_KEY)
    storage.set_mode(user_id, mode_key)

    mode_cfg = _get_mode_cfg(mode_key)
    text_to_send = txt.render_mode_changed(mode_cfg)

    await message.answer(
        text_to_send,
        parse_mode=ParseMode.HTML,
        **build_main_menu_answer_kwargs(),
    )


@router.message(F.text == BTN_BACK)
async def handle_back_to_main(message: Message):
    await _ensure_user(message)
    await message.answer(
        txt.render_back_to_main(),
        parse_mode=ParseMode.HTML,
        reply_markup=build_main_keyboard(),
    )


# --- Подписка ---------------------------------------------------------


@router.message(F.text == BTN_SUBSCRIPTION)
@router.message(Command("subscription"))
async def handle_subscription_root(message: Message):
    await _ensure_user(message)
    user_id = message.from_user.id

    plan = storage.get_plan(user_id)
    limits = storage.get_limits(user_id)

    text_to_send = txt.render_subscription_root(
        limits=limits,
        plan=plan,
        tariffs=SUBSCRIPTION_TARIFFS,
    )

    await message.answer(
        text_to_send,
        parse_mode=ParseMode.HTML,
        reply_markup=build_subscription_keyboard(),
    )


@router.message(F.text.in_(list(SUB_BUTTONS.keys())))
async def handle_subscription_select(message: Message):
    await _ensure_user(message)
    user_id = message.from_user.id

    btn = message.text
    plan_key = SUB_BUTTONS.get(btn)
    tariff = SUBSCRIPTION_TARIFFS.get(plan_key)

    if not tariff:
        await message.answer(
            txt.render_subscription_not_available(),
            parse_mode=ParseMode.HTML,
            **build_main_menu_answer_kwargs(),
        )
        return

    # Создаём счёт через CryptoBot
    try:
        invoice = await create_cryptobot_invoice(
            plan_key=plan_key,
            user_id=user_id,
        )
    except Exception as e:
        logger.exception("Failed to create cryptobot invoice: %s", e)
        await message.answer(
            txt.render_payment_error(),
            parse_mode=ParseMode.HTML,
            **build_main_menu_answer_kwargs(),
        )
        return

    text_to_send = txt.render_subscription_invoice(tariff=tariff, invoice=invoice)

    await message.answer(
        text_to_send,
        parse_mode=ParseMode.HTML,
        reply_markup=build_subscription_keyboard(),
    )


# --- Рефералы ---------------------------------------------------------


@router.message(F.text == BTN_REFERRALS)
@router.message(Command("ref"))
async def handle_referrals(message: Message):
    user = await _ensure_user(message)
    user_id = message.from_user.id

    stats = storage.get_referral_stats(user_id)
    referral_link = f"{REF_BASE_URL}?start={user['ref_code']}"

    text_to_send = txt.render_referrals(
        stats=stats,
        referral_link=referral_link,
    )

    await message.answer(
        text_to_send,
        parse_mode=ParseMode.HTML,
        **build_main_menu_answer_kwargs(),
    )


# --- Обработка свободного текста (LLM) -------------------------------


@router.message(F.text)
async def handle_user_prompt(message: Message):
    await _ensure_user(message)
    user_id = message.from_user.id
    text_input = (message.text or "").strip()

    # Всё, что является кнопками/служебным, уже разобрано выше
    if (
        text_input in MAIN_MENU_BUTTONS
        or text_input in MODE_BUTTONS
        or text_input in SUB_BUTTONS
        or text_input == BTN_BACK
    ):
        return

    if not text_input:
        await message.answer(
            txt.render_empty_prompt_error(),
            parse_mode=ParseMode.HTML,
            **build_main_menu_answer_kwargs(),
        )
        return

    # Ограничение по длине входа (символы / "токены" для простоты)
    if len(text_input) > MAX_INPUT_TOKENS:
        await message.answer(
            txt.render_too_long_error(MAX_INPUT_TOKENS),
            parse_mode=ParseMode.HTML,
            **build_main_menu_answer_kwargs(),
        )
        return

    limits = storage.get_limits(user_id)
    if limits["remaining_daily"] <= 0:
        await message.answer(
            txt.render_daily_limit_reached(limits),
            parse_mode=ParseMode.HTML,
            **build_main_menu_answer_kwargs(),
        )
        return

    mode_key = storage.get_mode(user_id) or DEFAULT_MODE_KEY

    # Сообщение "печатаю..."
    typing_msg = await message.answer(
        txt.render_thinking_message(),
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )

    # Обновляем статистику запросов
    storage.update_on_request(user_id, mode_key)

    full_answer_chunks = []
    try:
        async for chunk in ask_llm_stream(
            mode_key=mode_key,
            user_prompt=text_input,
            history=storage.get_chat_history(user_id),
        ):
            full_answer_chunks.append(chunk)
            text_to_show = "".join(full_answer_chunks)
            text_to_show = txt.normalize_model_answer(text_to_show)
            try:
                await typing_msg.edit_text(
                    text_to_show,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                # Игнорируем мелкие ошибки редактирования
                pass

        answer_text = "".join(full_answer_chunks).strip()
        if answer_text:
            storage.append_chat_history(
                user_id,
                role="assistant",
                content=answer_text,
            )
    except Exception as e:
        logger.exception("LLM error: %s", e)
        try:
            await typing_msg.edit_text(
                txt.render_generic_error(),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        try:
            await typing_msg.edit_reply_markup(reply_markup=build_main_keyboard())
        except Exception:
            pass


# --- Запуск -----------------------------------------------------------


async def main():
    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Starting bot polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
