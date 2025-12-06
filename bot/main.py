from __future__ import annotations

import asyncio
import logging
from typing import Optional, Dict, Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.client.default import DefaultBotProperties

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
from services.llm import generate_answer
from services.storage import Storage, UserRecord
from services.payments import create_cryptobot_invoice, get_invoice_status
from services import texts as txt  # важно: services.texts

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# --- Текст кнопок таскбара / режимов / подписки ---

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

BTN_SUB_1M = "💎 1 месяц"
BTN_SUB_3M = "💎 3 месяца"
BTN_SUB_12M = "💎 12 месяцев"
BTN_SUB_CHECK = "🔍 Проверить оплату"

# --- Разметка клавиатур ---

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MODES), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_SUBSCRIPTION), KeyboardButton(text=BTN_REFERRALS)],
    ],
    resize_keyboard=True,
)

MODES_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text=BTN_MODE_UNIVERSAL),
            KeyboardButton(text=BTN_MODE_MEDICINE),
        ],
        [
            KeyboardButton(text=BTN_MODE_COACH),
            KeyboardButton(text=BTN_MODE_BUSINESS),
        ],
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

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
)
dp = Dispatcher()
router = Router()
storage = Storage()

# Внутренняя память для режима «Продолжи»
CONTINUATIONS: Dict[int, Dict[str, Any]] = {}


# --- Вспомогательные функции ---


def _plan_title(plan_code: str, is_admin: bool) -> str:
    if is_admin or plan_code == "admin":
        return "Admin"
    if plan_code == "premium":
        return "Premium"
    return "Базовый"


def _mode_title(mode_key: str) -> str:
    cfg: Dict[str, Any] = ASSISTANT_MODES.get(mode_key) or ASSISTANT_MODES[DEFAULT_MODE_KEY]
    return cfg["title"]


def _estimate_prompt_tokens(text: str) -> int:
    # Грубая оценка: 1 токен ~ 4 символа
    return max(1, len(text) // 4)


def _check_limits(user: UserRecord, plan_code: str, is_admin: bool) -> Optional[str]:
    """Проверка лимитов по тарифу. Возвращает причину блокировки или None."""
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


def _build_style_hint(text: str) -> str:
    """Грубое описание стиля пользователя для LLM."""
    t = (text or "").strip()
    lower = t.lower()

    hints = []

    # Язык
    if any("а" <= ch.lower() <= "я" for ch in t if ch.isalpha()):
        hints.append("Отвечай по-русски.")
    else:
        hints.append("Отвечай на том языке, на котором задаёт вопросы пользователь.")

    # Тон
    if any(w in lower for w in ("ты", "бро", "брат", "саня", "дружище")):
        hints.append(
            "Обращайся к пользователю на «ты», дружелюбно и уважительно, без панибратства."
        )
    else:
        hints.append("Тон — спокойный, уважительный, без бюрократических оборотов.")

    # Стиль
    hints.append(
        "Текст делай премиальным: чёткая структура, минимум воды, аккуратная Markdown-разметка и без лишних эмодзи."
    )

    return " ".join(hints)


async def _continue_last_answer(message: Message, user: UserRecord) -> None:
    data = CONTINUATIONS.get(user.id)
    if not data:
        await message.answer(
            "Мне пока нечего продолжать — просто задай новый вопрос.",
            reply_markup=MAIN_KB,
        )
        return

    full_text: str = data.get("full_text") or ""
    cut_offset: int = int(data.get("cut_offset", 0))

    if not full_text or cut_offset >= len(full_text):
        await message.answer(
            "Похоже, я уже выдал полный ответ. Можем двигаться дальше.",
            reply_markup=MAIN_KB,
        )
        CONTINUATIONS.pop(user.id, None)
        return

    remainder = full_text[cut_offset:].lstrip()
    if not remainder:
        await message.answer(
            "Похоже, я уже выдал полный ответ. Можем двигаться дальше.",
            reply_markup=MAIN_KB,
        )
        CONTINUATIONS.pop(user.id, None)
        return

    typing_msg = await message.answer("✏️ Продолжаю мысль...", reply_markup=MAIN_KB)

    assembled = ""
    for block in remainder.split("\n\n"):
        block = block.strip()
        if not block:
            continue

        delta = ("\n\n" if assembled else "") + block

        if len(assembled) + len(delta) > 4000:
            available = 4000 - len(assembled)
            if available <= 0:
                break
            delta = delta[: max(0, available - 1)] + "…"
            assembled += delta
            try:
                await typing_msg.edit_text(assembled)
            except Exception as e:
                logger.debug("Failed to edit message while continuing (limit): %s", e)
            break

        assembled += delta
        try:
            await typing_msg.edit_text(assembled)
        except Exception as e:
            logger.debug("Failed to edit message while continuing: %s", e)
            break

        await asyncio.sleep(0.04)

    CONTINUATIONS.pop(user.id, None)


async def _send_streaming_answer(message: Message, user: UserRecord, text: str) -> None:
    """Живое печатание 2.0: структура + плавный стриминг + поддержка «Продолжи»."""
    typing_msg = await message.answer("⌛ Думаю...", reply_markup=MAIN_KB)

    # Обновляем и сохраняем стиль пользователя
    style_hint = _build_style_hint(text)
    user.style_hint = style_hint
    storage.save_user(user)

    try:
        answer = await generate_answer(
            mode_key=user.mode_key or DEFAULT_MODE_KEY,
            user_prompt=text,
            history=None,
            style_hint=style_hint,
        )

        assembled = ""
        base_sleep = 0.02 if answer.meta.get("answer_mode") == "quick" else 0.04

        for ch in answer.chunks:
            sep = "\n\n" if assembled else ""
            delta = sep + ch.text

            if len(assembled) + len(delta) > 4000:
                available = 4000 - len(assembled)
                if available <= 0:
                    break
                delta = delta[: max(0, available - 1)] + "…"
                assembled += delta
                try:
                    await typing_msg.edit_text(assembled)
                except Exception as e:
                    logger.debug(
                        "Failed to edit message while streaming (limit): %s", e
                    )
                break

            assembled += delta
            try:
                await typing_msg.edit_text(assembled)
            except Exception as e:
                logger.debug("Failed to edit message while streaming: %s", e)
                break

            await asyncio.sleep(base_sleep)

        if not answer.chunks:
            assembled = answer.full_text
            if len(assembled) > 4000:
                assembled = assembled[:3990] + "…"
            try:
                await typing_msg.edit_text(assembled)
            except Exception as e:
                logger.debug(
                    "Failed to edit message for single-chunk answer: %s", e
                )

        tokens = answer.meta.get("tokens", 0)
        storage.apply_usage(user, tokens)

        # Логика «Продолжи»
        user_id = user.id
        if answer.has_more or answer.meta.get("truncated"):
            full_text_meta = answer.meta.get("full_text")
            full_text = full_text_meta or answer.full_text
            cut_offset = int(answer.meta.get("cut_offset", len(full_text)))

            if full_text and cut_offset < len(full_text):
                CONTINUATIONS[user_id] = {
                    "full_text": full_text,
                    "cut_offset": cut_offset,
                }

                note = "\n\n_Если захочешь глубже — напиши «Продолжи»._"
                text_with_note = (
                    assembled + note if assembled else answer.full_text + note
                )

                if len(text_with_note) <= 4000:
                    try:
                        await typing_msg.edit_text(text_with_note)
                    except Exception as e:
                        logger.debug("Failed to append 'Продолжи' note: %s", e)
        else:
            CONTINUATIONS.pop(user.id, None)

    except Exception as e:
        logger.exception("LLM error: %s", e)
        try:
            await typing_msg.edit_text(txt.render_generic_error())
        except Exception:
            await message.answer(txt.render_generic_error(), reply_markup=MAIN_KB)


def _tariff_key_by_button(button_text: str) -> Optional[str]:
    """Маппинг текста кнопки → tariff_key из SUBSCRIPTION_TARIFFS."""
    mapping = {
        BTN_SUB_1M: "month_1",
        BTN_SUB_3M: "month_3",
        BTN_SUB_12M: "month_12",
    }
    return mapping.get(button_text)


# --- Хендлеры ---


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    full_text = message.text or ""
    parts = full_text.split(maxsplit=1)
    start_param = parts[1].strip() if len(parts) > 1 else ""

    user, created = storage.get_or_create_user(user_id, message.from_user)

    # Реферальный старт
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

    logger.info(
        "User %s started bot (created=%s, plan=%s, mode=%s)",
        user_id,
        created,
        plan_code,
        user.mode_key,
    )


@router.message(F.text == BTN_BACK_MAIN)
async def on_back_main(message: Message) -> None:
    await message.answer(
        "**Главный экран.**\n\nВыбери раздел внизу или просто задай вопрос.",
        reply_markup=MAIN_KB,
    )


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


@router.message(F.text.contains("Режимы"))
async def on_modes_root(message: Message) -> None:
    """
    Открывает экран выбора режимов.
    Фильтр по подстроке — чтобы сработало даже если в кнопке есть эмодзи или лишние пробелы.
    """
    text_body = txt.render_modes_root()
    await message.answer(text_body, reply_markup=MODES_KB)


@router.message(
    F.text.in_(
        {
            BTN_MODE_UNIVERSAL,
            BTN_MODE_MEDICINE,
            BTN_MODE_COACH,
            BTN_MODE_BUSINESS,
            BTN_MODE_CREATIVE,
        }
    )
)
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

    await message.answer(
        txt.render_mode_switched(mode_title),
        reply_markup=MAIN_KB,
    )


@router.message(F.text == BTN_SUBSCRIPTION)
async def on_subscription(message: Message) -> None:
    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)

    is_admin = storage.is_admin(user_id)
    plan_code = storage.effective_plan(user, is_admin)
    plan_title = _plan_title(plan_code, is_admin)

    text_body = txt.render_subscription_overview(
        plan_title,
        user.premium_until,
    )
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
        await message.answer(
            txt.render_payment_check_result("not_found"),
            reply_markup=SUB_KB,
        )
        return

    status = await get_invoice_status(invoice_id)
    if not status:
        await message.answer(
            txt.render_payment_check_result("not_found"),
            reply_markup=SUB_KB,
        )
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

    text_body = txt.render_referrals(
        ref_link=ref_link,
        total_refs=user.referrals_count,
    )
    await message.answer(text_body, reply_markup=REF_KB)


@router.message(F.text.startswith("/"))
async def on_unknown_command(message: Message) -> None:
    await message.answer(
        "**Команда не распознана.**\n\n"
        "Используй нижние кнопки навигации или просто напиши запрос — "
        "я подстроюсь под формат.",
        reply_markup=MAIN_KB,
    )


@router.message(F.text.len() > 0)
async def on_user_message(message: Message) -> None:
    raw_text = message.text or ""
    text = raw_text.strip()

    if not text:
        await message.answer(txt.render_empty_prompt_error(), reply_markup=MAIN_KB)
        return

    user_id = message.from_user.id
    user, _ = storage.get_or_create_user(user_id, message.from_user)

    lower = text.casefold()
    if lower in {"продолжи", "продолжить", "дальше", "ещё", "еще"}:
        await _continue_last_answer(message, user)
        return

    if len(text) > MAX_INPUT_TOKENS * 4:
        await message.answer(txt.render_too_long_prompt_error(), reply_markup=MAIN_KB)
        return

    is_admin = storage.is_admin(user_id)
    plan_code = storage.effective_plan(user, is_admin)

    reason = _check_limits(user, plan_code, is_admin)
    if reason:
        await message.answer(
            txt.render_limits_warning(reason),
            reply_markup=MAIN_KB,
        )
        return

    await _send_streaming_answer(message, user, text)


async def main() -> None:
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
