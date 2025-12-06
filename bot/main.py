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
    DEFAULT_MODE,
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
@@ -103,191 +103,191 @@ def build_modes_keyboard() -> ReplyKeyboardMarkup:

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


def _get_mode_cfg(mode_key: str) -> Dict[str, Any]:␊
    return ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES[DEFAULT_MODE])


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

    mode_key = storage.get_mode(user_id) or DEFAULT_MODE
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

    mode_key = storage.get_mode(user_id) or DEFAULT_MODE
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

    mode_key = storage.get_mode(user_id) or DEFAULT_MODE
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
    mode_key = MODE_BUTTONS.get(btn_text, DEFAULT_MODE)
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


@@ -310,64 +310,72 @@ async def handle_subscription_root(message: Message):
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
            tariff_code=plan_key,
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

    if not invoice:
        await message.answer(
            txt.render_subscription_not_available(),
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

@@ -400,61 +408,61 @@ async def handle_user_prompt(message: Message):
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

    mode_key = storage.get_mode(user_id) or DEFAULT_MODE

    # Сообщение "печатаю..."
    typing_msg = await message.answer(
        txt.render_thinking_message(),
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )

    # Обновляем статистику запросов
    storage.update_on_request(user_id, mode_key, user_prompt=text_input)

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
