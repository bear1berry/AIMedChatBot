from __future__ import annotations

import asyncio
import logging
import os
import textwrap
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

from .subscription_db import (
    init_db,
    get_or_create_user,
    get_user_by_telegram_id,
    set_user_mode,
    set_user_note,
    get_user_note,
    get_free_usage_today,
    increment_usage,
    has_premium,
    ensure_referral_code,
    find_user_by_referral_code,
    add_referral,
    get_user_referrals,
    grant_premium_days,
    create_invoice_record,
    get_last_invoice_for_user,
    mark_invoice_paid,
)
from .payments_crypto import create_invoice, fetch_invoice_status

# ---------------------------------------------------------------------------
# Base config
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

FREE_MESSAGES_LIMIT = int(os.getenv("FREE_MESSAGES_LIMIT", "20"))

ADMIN_USERNAMES = {
    u.strip().lower()
    for u in os.getenv("ADMIN_USERNAMES", "").replace(",", " ").split()
    if u.strip()
}

BOT_USERNAME = os.getenv("BOT_USERNAME", "BlackBoxGPT_bot")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Aiogram setup
# ---------------------------------------------------------------------------

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
    ),
)

# ---------------------------------------------------------------------------
# UI constants
# ---------------------------------------------------------------------------

BTN_NEW = "💡 Новый запрос"
BTN_MODE = "🎛 Режим"
BTN_PROFILE = "📂 Профиль"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_REFERRALS = "👥 Рефералы"

BTN_MEMORY_EDIT = "✏️ Обновить досье"
BTN_MEMORY_SHOW = "📖 Мое досье"
BTN_BACK = "⬅️ Назад"

BTN_MODE_UNI = "🌍 Универсальный"
BTN_MODE_FOCUS = "🎯 Фокус / Задачи"
BTN_MODE_DEEP = "🧠 Глубокий разбор"
BTN_MODE_CREATIVE = "🔥 Креатив / Идеи"
BTN_MODE_MENTOR = "📣 Ментор / Мотивация"

BTN_PLAN_1M = "1 месяц — 5 USDT"
BTN_PLAN_3M = "3 месяца — 12 USDT"
BTN_PLAN_12M = "12 месяцев — 60 USDT"
BTN_SUB_CHECK = "🔁 Проверить оплату"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_MODE)],
        [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_SUBSCRIPTION)],
        [KeyboardButton(text=BTN_REFERRALS)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Спроси о чём угодно…",
)

MEMORY_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MEMORY_EDIT), KeyboardButton(text=BTN_MEMORY_SHOW)],
        [KeyboardButton(text=BTN_BACK)],
    ],
    resize_keyboard=True,
)

MODES_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_MODE_UNI), KeyboardButton(text=BTN_MODE_FOCUS)],
        [KeyboardButton(text=BTN_MODE_DEEP), KeyboardButton(text=BTN_MODE_CREATIVE)],
        [KeyboardButton(text=BTN_MODE_MENTOR)],
        [KeyboardButton(text=BTN_BACK)],
    ],
    resize_keyboard=True,
)

SUBSCRIPTION_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_PLAN_1M)],
        [KeyboardButton(text=BTN_PLAN_3M)],
        [KeyboardButton(text=BTN_PLAN_12M)],
        [KeyboardButton(text=BTN_SUB_CHECK), KeyboardButton(text=BTN_BACK)],
    ],
    resize_keyboard=True,
)

REFERRAL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_BACK)]],
    resize_keyboard=True,
)

# пользователи, которые сейчас пишут новое досье
EDITING_NOTE_USERS: set[int] = set()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_available() -> bool:
    return bool(DEEPSEEK_API_KEY or GROQ_API_KEY)


async def _warn_if_no_llm(message: Message) -> None:
    if _llm_available():
        return
    text = textwrap.dedent(
        """
        ⚠️ <b>Ключи для LLM не настроены.</b>
        Добавь в <code>.env</code> переменные <code>DEEPSEEK_API_KEY</code> или <code>GROQ_API_KEY</code>.
        """
    ).strip()
    await message.answer(text)


def _mode_to_label(mode: str | None) -> str:
    mapping = {
        "universal": BTN_MODE_UNI,
        "focus": BTN_MODE_FOCUS,
        "deep": BTN_MODE_DEEP,
        "creative": BTN_MODE_CREATIVE,
        "mentor": BTN_MODE_MENTOR,
    }
    return mapping.get(mode or "universal", BTN_MODE_UNI)


def _label_to_mode(label: str) -> Optional[str]:
    mapping = {
        BTN_MODE_UNI: "universal",
        BTN_MODE_FOCUS: "focus",
        BTN_MODE_DEEP: "deep",
        BTN_MODE_CREATIVE: "creative",
        BTN_MODE_MENTOR: "mentor",
    }
    return mapping.get(label)


async def _ensure_user(message: Message) -> Dict[str, Any]:
    username = (message.from_user.username or "").lower() if message.from_user else ""
    full_name = message.from_user.full_name if message.from_user else ""
    telegram_id = message.from_user.id if message.from_user else 0

    try:
        user = get_or_create_user(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            is_admin=username in ADMIN_USERNAMES,
        )
    except TypeError:
        # совместимость, если функция не принимает is_admin
        user = get_or_create_user(telegram_id, username, full_name)
    return user


async def _check_limit(message: Message) -> bool:
    """
    True  -> можно продолжать (лимит не исчерпан или premium).
    False -> лимит закончился, ответ уже отправлен пользователю.
    """
    telegram_id = message.from_user.id
    if has_premium(telegram_id):
        return True

    used = get_free_usage_today(telegram_id)
    if used >= FREE_MESSAGES_LIMIT:
        text = textwrap.dedent(
            f"""
            😔 Бесплатный лимит исчерпан.

            Сегодня ты уже отправил <b>{FREE_MESSAGES_LIMIT}</b> сообщений.
            Чтобы продолжить без ограничений — оформи 💎 <b>BlackBox GPT Premium</b>.

            Нажми кнопку <b>«{BTN_SUBSCRIPTION}»</b> внизу, чтобы посмотреть тарифы.
            """
        ).strip()
        await message.answer(text, reply_markup=MAIN_KB)
        return False

    new_used = increment_usage(telegram_id)
    logger.info(
        "User %s used free message #%s / %s",
        telegram_id,
        new_used,
        FREE_MESSAGES_LIMIT,
    )
    if new_used == FREE_MESSAGES_LIMIT:
        await message.answer(
            f"ℹ️ Это было твоё <b>{FREE_MESSAGES_LIMIT}</b>-е бесплатное сообщение на сегодня. "
            f"Следующее потребует Premium-подписку.",
            reply_markup=MAIN_KB,
        )
    return True


def _build_system_prompt(mode: str) -> str:
    base = (
        "Ты — BlackBox GPT, универсальный русскоязычный ассистент. "
        "Отвечай ясно, структурированно и по делу. "
        "Всегда учитывай контекст диалога, но не выдумывай факты."
    )
    if mode == "focus":
        extra = (
            "Сейчас активен режим фокуса и задач. "
            "Помогай раскладывать цели на шаги, предлагай конкретные действия и дедлайны."
        )
    elif mode == "deep":
        extra = (
            "Сейчас активен режим глубокого разбора. "
            "Задавай уточняющие вопросы, анализируй причины и последствия, давай развёрнутую аналитику."
        )
    elif mode == "creative":
        extra = (
            "Сейчас активен режим креатива и идей. "
            "Предлагай необычные, дерзкие и при этом практичные варианты. Можно чуть более свободный стиль."
        )
    elif mode == "mentor":
        extra = (
            "Сейчас активен режим наставника и мотивации. "
            "Говори жёстко по делу, но с поддержкой. Подсвечивай сильные стороны пользователя и точки роста."
        )
    else:
        extra = (
            "Базовый режим — универсальный помощник. "
            "Краткость приветствуется, но не в ущерб сути."
        )
    return base + " " + extra


async def _call_llm(user_id: int, mode: str, user_prompt: str) -> str:
    system_prompt = _build_system_prompt(mode)

    if DEEPSEEK_API_KEY:
        url = "https://api.deepseek.com/chat/completions"
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
    elif GROQ_API_KEY:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "llama-3.1-70b-versatile",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
    else:
        raise RuntimeError("No LLM API key configured")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to parse LLM response: %s", exc)
        return "⚠️ Не удалось разобрать ответ модели. Попробуй переформулировать запрос."


async def _process_referral_start(message: Message, payload: str) -> None:
    """
    Обрабатываем deep-link /start <ref_code>.
    Добавляем реферала и начисляем по 1 дню premium обоим.
    """
    code = payload.strip()
    if not code:
        return

    try:
        referrer = find_user_by_referral_code(code)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to lookup referrer by code %s", code)
        return

    if not referrer:
        logger.info("Referral code %s not found", code)
        return

    me_id = message.from_user.id
    if referrer["telegram_id"] == me_id:
        logger.info("User %s tried to use own referral code", me_id)
        return

    # Создаём пользователя, если его ещё нет
    _ = await _ensure_user(message)

    try:
        added = add_referral(
            referrer_telegram_id=referrer["telegram_id"],
            referred_telegram_id=me_id,
        )
        if not added:
            # уже был такой реферал, ничего не даём
            return
    except TypeError:
        # совместимость, если add_referral принимает другие аргументы
        add_referral(referrer["telegram_id"], me_id)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to add referral relation")
        return

    # Начисляем по 1 дню premium
    try:
        grant_premium_days(referrer["telegram_id"], days=1)
        grant_premium_days(me_id, days=1)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to grant referral premium days")
        return

    await message.answer(
        "🎁 <b>Реферальный бонус активирован.</b>\n"
        "Тебе и другу начислено по <b>1 дню</b> Premium-доступа.",
        reply_markup=MAIN_KB,
    )


# ---------------------------------------------------------------------------
# Handlers: commands & menus
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    # deep-link payload
    payload = None
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()

    await _ensure_user(message)

    if payload:
        await _process_referral_start(message, payload)

    user_row = get_user_by_telegram_id(message.from_user.id)
    mode_label = _mode_to_label(user_row.get("mode") if user_row else "universal")

    is_premium = has_premium(message.from_user.id)
    status = "💎 <b>Premium</b>" if is_premium else "🆓 Бесплатный режим"

    text = textwrap.dedent(
        f"""
        Привет, {message.from_user.first_name or "друг"} 👾

        Это <b>BlackBox GPT</b> — универсальный AI-ассистент, который помогает с:
        • задачами и фокусом,
        • идеями и креативом,
        • анализом ситуаций,
        • личной стратегией и мотивацией.

        Текущий режим: <b>{mode_label}</b>
        Статус: {status}

        Просто напиши запрос или нажми «{BTN_NEW}».
        Все основные действия — в нижнем меню.
        """
    ).strip()

    await message.answer(text, reply_markup=MAIN_KB)
    await _warn_if_no_llm(message)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = textwrap.dedent(
        f"""
        📚 <b>Как пользоваться BlackBox GPT</b>

        1️⃣ Напиши любой вопрос — от бытового до кода.
        2️⃣ Используй нижнее меню:
           • {BTN_NEW} — начать новый запрос.
           • {BTN_MODE} — переключить стиль ответов.
           • {BTN_PROFILE} — профиль и личное досье.
           • {BTN_SUBSCRIPTION} — подписка и лимиты.
           • {BTN_REFERRALS} — реферальная программа.

        Дополнительно доступны команды:
        /start — перезапустить приветствие
        /modes — выбор режима
        /profile — профиль и досье
        /subscription — тарифы
        /ref — реферальная система
        """
    ).strip()
    await message.answer(text, reply_markup=MAIN_KB)


@router.message(Command("modes"))
async def cmd_modes(message: Message) -> None:
    await show_modes(message)


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    await profile_menu(message)


@router.message(Command("subscription"))
async def cmd_subscription_cmd(message: Message) -> None:
    await show_subscription(message)


@router.message(Command("ref"))
async def cmd_ref(message: Message) -> None:
    await referral_menu(message)


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    await cmd_start(message)


# ---------------------------------------------------------------------------
# Main menu buttons
# ---------------------------------------------------------------------------


@router.message(F.text == BTN_NEW)
async def on_new_request(message: Message) -> None:
    await _ensure_user(message)
    text = textwrap.dedent(
        f"""
        🧹 <b>Новый запрос.</b>

        Опиши одной фразой, что тебе нужно:
        • решить задачу,
        • разобрать ситуацию,
        • придумать идеи,
        • получить мотивационный разбор.

        Я подстроюсь под выбранный режим. Если нужно — поменяй его через «{BTN_MODE}».
        """
    ).strip()
    await message.answer(text, reply_markup=MAIN_KB)
    await _warn_if_no_llm(message)


@router.message(F.text == BTN_MODE)
async def on_mode_menu(message: Message) -> None:
    await show_modes(message)


@router.message(F.text == BTN_PROFILE)
async def on_profile_menu(message: Message) -> None:
    await profile_menu(message)


@router.message(F.text == BTN_SUBSCRIPTION)
async def on_subscription_menu(message: Message) -> None:
    await show_subscription(message)


@router.message(F.text == BTN_REFERRALS)
async def on_referrals_menu(message: Message) -> None:
    await referral_menu(message)


@router.message(F.text == BTN_BACK)
async def on_back(message: Message) -> None:
    # Всегда возвращаемся в главное меню
    await cmd_start(message)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


async def show_modes(message: Message) -> None:
    user = await _ensure_user(message)
    current = _mode_to_label(user.get("mode"))

    text = textwrap.dedent(
        f"""
        🎛 <b>Режимы ассистента</b>

        Сейчас активен: <b>{current}</b>.

        Выбери стиль, в котором я буду работать:

        {BTN_MODE_UNI}
        — баланс скорости и глубины. По любым темам.

        {BTN_MODE_FOCUS}
        — разбор задач, планы, фокус, приоритеты.

        {BTN_MODE_DEEP}
        — детальный анализ, причины, стратегии, системное мышление.

        {BTN_MODE_CREATIVE}
        — идеи, сценарии, формулировки, нестандартные подходы.

        {BTN_MODE_MENTOR}
        — личная сила, мотивация, внутренний стержень, разговор «как есть».

        Можно в любой момент менять режим — это влияет только на стиль и глубину ответа.
        """
    ).strip()

    await message.answer(text, reply_markup=MODES_KB)


@router.message(
    F.text.in_(
        {BTN_MODE_UNI, BTN_MODE_FOCUS, BTN_MODE_DEEP, BTN_MODE_CREATIVE, BTN_MODE_MENTOR}
    )
)
async def on_mode_selected(message: Message) -> None:
    mode = _label_to_mode(message.text)
    if not mode:
        await message.answer(
            "Не удалось распознать режим. Попробуй ещё раз.", reply_markup=MODES_KB
        )
        return

    set_user_mode(message.from_user.id, mode)
    await message.answer(
        f"✅ Режим переключён на: <b>{_mode_to_label(mode)}</b>.\n"
        f"Теперь просто задай вопрос.",
        reply_markup=MAIN_KB,
    )


# ---------------------------------------------------------------------------
# Profile & memory
# ---------------------------------------------------------------------------


async def profile_menu(message: Message) -> None:
    user = await _ensure_user(message)
    telegram_id = message.from_user.id

    is_premium = has_premium(telegram_id)
    used = get_free_usage_today(telegram_id)
    mode_label = _mode_to_label(user.get("mode"))
    note = get_user_note(telegram_id)

    if is_premium:
        premium_until_ts = user.get("premium_until")
        if premium_until_ts:
            dt = datetime.fromtimestamp(premium_until_ts, tz=timezone.utc)
            premium_until_str = dt.strftime("%d.%m.%Y")
        else:
            premium_until_str = "без срока (lifetime)"
        premium_status = f"💎 <b>Premium</b> до <b>{premium_until_str}</b>"
    else:
        premium_status = "🆓 Бесплатный режим"

    text = textwrap.dedent(
        f"""
        📂 <b>Профиль</b>

        ID: <code>{telegram_id}</code>
        Режим: <b>{mode_label}</b>
        Статус: {premium_status}
        Бесплатный лимит: <b>{used} / {FREE_MESSAGES_LIMIT}</b> сообщений на сегодня.

        🧠 <b>Личное досье</b>
        Я могу запомнить про тебя важные вещи: цели, контекст, особенности.

        • {BTN_MEMORY_EDIT} — переписать досье.
        • {BTN_MEMORY_SHOW} — показать, что уже сохранено.

        Текущее досье (кратко):
        {note if note else "— пока пусто."}
        """
    ).strip()

    await message.answer(text, reply_markup=MEMORY_KB)


@router.message(F.text == BTN_MEMORY_EDIT)
async def on_memory_edit(message: Message) -> None:
    await _ensure_user(message)
    user_id = message.from_user.id
    EDITING_NOTE_USERS.add(user_id)

    text = textwrap.dedent(
        """
        ✏️ <b>Обновление личного досье.</b>

        Напиши одним сообщением то, что мне важно о тебе помнить:
        • кто ты и чем занимаешься;
        • твои ключевые цели;
        • важные ограничения / особенности.

        Я перезапишу досье целиком этим текстом.
        """
    ).strip()
    await message.answer(
        text,
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=BTN_BACK)]],
            resize_keyboard=True,
        ),
    )


@router.message(F.text == BTN_MEMORY_SHOW)
async def on_memory_show(message: Message) -> None:
    await _ensure_user(message)
    note = get_user_note(message.from_user.id)
    if note:
        text = textwrap.dedent(
            f"""
            📖 <b>Твоё личное досье</b>

            {note}
            """
        ).strip()
    else:
        text = (
            "📖 Личное досье пока пустое. Нажми «✏️ Обновить досье», "
            "чтобы я запомнил о тебе главное."
        )
    await message.answer(text, reply_markup=MEMORY_KB)


# ---------------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------------


async def referral_menu(message: Message) -> None:
    user = await _ensure_user(message)
    telegram_id = message.from_user.id

    # гарантируем наличие кода
    try:
        code = ensure_referral_code(telegram_id)
    except TypeError:
        # совместимость, если функция принимает user_id
        code = ensure_referral_code(user["id"])
    except Exception:  # noqa: BLE001
        logger.exception("Failed to ensure referral code, generating fallback one")
        code = f"BBX{telegram_id}"

    link = f"https://t.me/{BOT_USERNAME}?start={code}"

    try:
        referrals = get_user_referrals(telegram_id)
        total = len(referrals)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to fetch referrals list")
        referrals = []
        total = 0

    text = textwrap.dedent(
        f"""
        👥 <b>Реферальная программа</b>

        Твоя личная ссылка:
        <code>{link}</code>

        За каждого друга, который зайдёт по ссылке:
        • Тебе — <b>+1 день</b> BlackBox GPT Premium
        • Ему — <b>+1 день</b> Premium на старте

        Уже приглашено: <b>{total}</b> человек(а).

        Просто отправь ссылку тем, кому нужен умный ассистент.
        """
    ).strip()

    await message.answer(text, reply_markup=REFERRAL_KB)


# ---------------------------------------------------------------------------
# Subscription & payments
# ---------------------------------------------------------------------------


PLANS = {
    BTN_PLAN_1M: {"code": "p1m", "days": 30, "price": 5.0},
    BTN_PLAN_3M: {"code": "p3m", "days": 90, "price": 12.0},
    BTN_PLAN_12M: {"code": "p12m", "days": 365, "price": 60.0},
}


async def show_subscription(message: Message) -> None:
    await _ensure_user(message)
    is_premium = has_premium(message.from_user.id)

    status = (
        "💎 <b>Premium активен.</b>"
        if is_premium
        else "🆓 Сейчас у тебя базовый бесплатный доступ."
    )

    text = textwrap.dedent(
        f"""
        ⚡️ <b>Подписка BlackBox GPT Premium</b>

        Бесплатный лимит — <b>{FREE_MESSAGES_LIMIT}</b> сообщений в день.
        После — безлимитный доступ по подписке.

        {status}

        Тарифы:
        • 1 месяц — 5 USDT
        • 3 месяца — 12 USDT
        • 12 месяцев — 60 USDT

        Выбери тариф внизу, я создам счёт в Crypto Bot.
        """
    ).strip()

    await message.answer(text, reply_markup=SUBSCRIPTION_KB)


@router.message(F.text.in_({BTN_PLAN_1M, BTN_PLAN_3M, BTN_PLAN_12M}))
async def on_plan_selected(message: Message) -> None:
    await _ensure_user(message)
    plan = PLANS.get(message.text)
    if not plan:
        await message.answer(
            "Не удалось определить тариф. Попробуй ещё раз.",
            reply_markup=SUBSCRIPTION_KB,
        )
        return

    telegram_id = message.from_user.id
    plan_code = plan["code"]
    days = plan["days"]
    price = plan["price"]

    try:
        invoice = await create_invoice(telegram_id, plan_code, price, days)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error while creating invoice: %s", exc)
        await message.answer(
            "😔 Не удалось создать счёт из-за непредвиденной ошибки. Попробуй ещё раз чуть позже.",
            reply_markup=SUBSCRIPTION_KB,
        )
        return

    invoice_id = str(invoice.get("invoice_id"))
    pay_url = invoice.get("pay_url")

    try:
        create_invoice_record(
            invoice_id=invoice_id,
            telegram_id=telegram_id,
            plan_code=plan_code,
            amount_usdt=price,
            period_days=days,
            pay_url=pay_url,
        )
    except TypeError:
        # совместимость, если сигнатура без pay_url
        create_invoice_record(invoice_id, telegram_id, plan_code, price, days)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record invoice in DB")

    text = textwrap.dedent(
        f"""
        💳 <b>Счёт на оплату создан.</b>

        Тариф: <b>{message.text}</b>
        Сумма: <b>{price} USDT</b>

        Чтобы оплатить, просто перейди по ссылке:
        {pay_url}

        После оплаты вернись в бот и нажми «{BTN_SUB_CHECK}».
        """
    ).strip()

    await message.answer(text, reply_markup=SUBSCRIPTION_KB)


@router.message(F.text == BTN_SUB_CHECK)
async def on_check_payment(message: Message) -> None:
    telegram_id = message.from_user.id
    invoice = get_last_invoice_for_user(telegram_id)
    if not invoice:
        await message.answer(
            "Пока не вижу ни одного созданного счёта. Сначала выбери тариф, чтобы я создал счёт.",
            reply_markup=SUBSCRIPTION_KB,
        )
        return

    invoice_id = str(invoice["invoice_id"])

    try:
        status = await fetch_invoice_status(invoice_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error while checking invoice status: %s", exc)
        await message.answer(
            "⚠️ Не удалось проверить оплату. Попробуй ещё раз через минуту.",
            reply_markup=SUBSCRIPTION_KB,
        )
        return

    if status in {"paid", "finished"}:
        # отмечаем оплаченной и выдаём premium
        try:
            mark_invoice_paid(invoice_id)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to mark invoice %s as paid in DB", invoice_id)

        days = int(invoice.get("period_days", 30))
        try:
            grant_premium_days(telegram_id, days=days)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to grant premium after payment")

        await message.answer(
            "✅ Оплата найдена. Premium активирован!\n\n"
            "Теперь лимитов по сообщениям нет, можно использовать бота по-максимуму.",
            reply_markup=MAIN_KB,
        )
    elif status in {"active", "pending"}:
        await message.answer(
            "⏳ Оплата ещё не прошла. Если уже оплатил, подожди 10–20 секунд и попробуй снова.",
            reply_markup=SUBSCRIPTION_KB,
        )
    else:
        await message.answer(
            "😔 Счёт находится в статусе, при котором оплата недоступна или отменена. "
            "Если считаешь, что это ошибка, напиши администратору.",
            reply_markup=SUBSCRIPTION_KB,
        )


# ---------------------------------------------------------------------------
# Chat handler
# ---------------------------------------------------------------------------


@router.message(F.text)
async def handle_chat(message: Message) -> None:
    # если пользователь сейчас редактирует досье — сохраняем вместо ответа модели
    user_id = message.from_user.id
    if user_id in EDITING_NOTE_USERS and message.text not in {
        BTN_MEMORY_EDIT,
        BTN_MEMORY_SHOW,
        BTN_MODE,
        BTN_SUBSCRIPTION,
        BTN_PROFILE,
        BTN_REFERRALS,
        BTN_NEW,
        BTN_BACK,
    }:
        EDITING_NOTE_USERS.discard(user_id)
        set_user_note(user_id, message.text.strip())
        await message.answer(
            "✅ Досье обновлено. Я буду опираться на эту информацию в ответах.",
            reply_markup=MAIN_KB,
        )
        return

    # Игнорируем команды — для них есть отдельные хендлеры
    if message.text.startswith("/"):
        return

    await _ensure_user(message)

    if not _llm_available():
        await _warn_if_no_llm(message)
        return

    if not await _check_limit(message):
        return

    user_row = get_user_by_telegram_id(message.from_user.id)
    mode = (user_row or {}).get("mode", "universal")

    thinking = await message.answer("🤔 Думаю над ответом…", reply_markup=MAIN_KB)

    try:
        answer = await _call_llm(message.from_user.id, mode, message.text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM request failed: %s", exc)
        await thinking.edit_text(
            "⚠️ Что-то пошло не так при обращении к модели. Попробуй ещё раз чуть позже.",
        )
        return

    await thinking.edit_text(answer)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    logger.info("Initializing database…")
    init_db()
    logger.info("Starting BlackBox GPT bot polling…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


