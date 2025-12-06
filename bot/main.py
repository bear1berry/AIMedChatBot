# bot/main.py
from __future__ import annotations

import asyncio
import logging
import textwrap
from datetime import date, datetime, timedelta
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from bot.config import (
    ASSISTANT_MODES,
    BOT_TOKEN,
    DEFAULT_DAILY_LIMIT,
    DEFAULT_MODE,
    OWNER_ID,
    PLAN_BASIC,
    PLAN_PREMIUM,
    SUBSCRIPTION_TARIFFS,
)
from services.llm import generate_answer
from services.payments import create_cryptobot_invoice, get_invoice_status
from services.storage import Storage, UserRecord

# ---------------------------------------------------------------------------
# Логирование и инициализация
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

router = Router()
dp = Dispatcher()
dp.include_router(router)

storage = Storage()

# ---------------------------------------------------------------------------
# UI: кнопки и клавиатура
# ---------------------------------------------------------------------------

BTN_MODES = "🧠 Режимы"
BTN_PROFILE = "👤 Профиль"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_REFERRALS = "👥 Рефералы"


def build_main_keyboard() -> ReplyKeyboardMarkup:
    """
    Нижний таскбар. Никаких инлайнов — всё управление здесь.
    """
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODES), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_SUBSCRIPTION), KeyboardButton(text=BTN_REFERRALS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
    return kb


def _wrap(text: str) -> str:
    return textwrap.dedent(text).strip()


# ---------------------------------------------------------------------------
# Вспомогательные функции для UserRecord
# ---------------------------------------------------------------------------

def is_premium(user: UserRecord) -> bool:
    if user.tariff != PLAN_PREMIUM:
        return False
    if not user.premium_until:
        return False
    try:
        until = date.fromisoformat(user.premium_until)
    except ValueError:
        return False
    return until >= date.today()


def active_daily_limit(user: UserRecord) -> int:
    """
    Итоговый лимит на день: базовый + бонусы за рефералов.
    В storage уже лежат daily_limit и ref_bonus_limit, но на всякий случай
    считаем явно.
    """
    base = getattr(user, "daily_limit", DEFAULT_DAILY_LIMIT)
    bonus = getattr(user, "ref_bonus_limit", 0)
    return base + bonus


def refresh_usage_if_needed(user: UserRecord) -> None:
    """
    Обнуляем счётчик за сегодня, если дата поменялась.
    """
    today_str = date.today().isoformat()
    last_date = getattr(user, "last_usage_date", None)

    if last_date != today_str:
        user.last_usage_date = today_str
        user.used_today = 0  # type: ignore[attr-defined]


def register_usage(user: UserRecord) -> None:
    """
    Фиксируем один использованный запрос.
    """
    refresh_usage_if_needed(user)
    user.used_today = getattr(user, "used_today", 0) + 1  # type: ignore[attr-defined]
    user.total_requests = getattr(user, "total_requests", 0) + 1
    storage.update_user(user)


def remaining_requests(user: UserRecord) -> int:
    refresh_usage_if_needed(user)
    limit_today = active_daily_limit(user)
    used = getattr(user, "used_today", 0)
    return max(limit_today - used, 0)


def get_mode_title(user: UserRecord) -> str:
    mode_key = getattr(user, "mode_key", DEFAULT_MODE)
    mode_cfg = ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES[DEFAULT_MODE])
    emoji = mode_cfg.get("emoji", "🧠") or "🧠"
    title = mode_cfg.get("title", "Универсальный")
    return f"{emoji} {title}"


def set_mode(user: UserRecord, mode_key: str) -> None:
    if mode_key not in ASSISTANT_MODES:
        mode_key = DEFAULT_MODE
    user.mode_key = mode_key  # type: ignore[attr-defined]
    storage.update_user(user)


def ensure_user(message: Message) -> Tuple[UserRecord, bool]:
    assert message.from_user is not None
    user_id = message.from_user.id
    rec, created = storage.get_or_create_user(user_id, message.from_user)
    return rec, created


# ---------------------------------------------------------------------------
# Тексты (премиальный стиль)
# ---------------------------------------------------------------------------

def render_onboarding(user: UserRecord, bot_username: str) -> str:
    mode_label = get_mode_title(user)
    plan = "Premium" if is_premium(user) else "Базовый"
    limit_today = active_daily_limit(user)

    return _wrap(
        f"""
        👋 Привет. Я BlackBox GPT — Universal AI Assistant.

        Минималистичный интерфейс. Максимум мозга.

        Снизу — твой таскбар:
        • 🧠 Режимы — выбираешь мозг под задачу
        • 👤 Профиль — тариф, режим и лимиты
        • 💎 Подписка — Premium через USDT (CryptoBot)
        • 👥 Рефералы — бонусы за приглашения

        Сейчас активен режим: {mode_label}
        Тариф: {plan} • Лимит на сегодня: {limit_today} запросов.

        Просто напиши свой первый запрос — я подстроюсь под твой стиль общения.
        Если что-то забудешь — команда /help всегда рядом.
        """
    )


def render_profile(user: UserRecord) -> str:
    mode_label = get_mode_title(user)
    plan = "Premium" if is_premium(user) else "Базовый"
    limit_today = active_daily_limit(user)
    refresh_usage_if_needed(user)
    used = getattr(user, "used_today", 0)
    total = getattr(user, "total_requests", 0)
    premium_until = getattr(user, "premium_until", None)

    if is_premium(user) and premium_until:
        plan_line = f"{plan} (до {premium_until})"
    else:
        plan_line = plan

    return _wrap(
        f"""
        👤 Профиль

        Тариф: {plan_line}
        Режим: {mode_label}

        Лимит на сегодня: {used} / {limit_today} запросов
        Всего запросов за всё время: {total}

        Хочешь больше свободы и скорости — загляни в раздел «{BTN_SUBSCRIPTION}».
        """
    )


def render_modes(user: UserRecord) -> str:
    current_mode = get_mode_title(user)

    lines = [
        "🧠 Режимы мышления",
        "",
        f"Сейчас активен: {current_mode}",
        "",
        "Доступные режимы:",
    ]

    # Отображаем только ключевые поля, без избыточности.
    for key, cfg in ASSISTANT_MODES.items():
        emoji = cfg.get("emoji", "🧠") or "🧠"
        title = cfg.get("title", key)
        desc = cfg.get("description", "")
        lines.append(f"• /mode_{key} — {emoji} {title}")
        if desc:
            lines.append(f"  {desc}")

    lines.append("")
    lines.append(
        "Просто тапни по команде — Telegram сам подставит её в строку ввода."
    )

    return "\n".join(lines)


def render_subscription_overview() -> str:
    t1 = SUBSCRIPTION_TARIFFS["premium_1m"]
    t3 = SUBSCRIPTION_TARIFFS["premium_3m"]
    t12 = SUBSCRIPTION_TARIFFS["premium_12m"]

    return _wrap(
        f"""
        💎 Подписка

        Оплата — только в USDT через @CryptoBot.

        Тарифы:
        • /premium_1m  — Premium • 1 месяц  за {t1.price_usdt:.2f} USDT
        • /premium_3m  — Premium • 3 месяца за {t3.price_usdt:.2f} USDT
        • /premium_12m — Premium • 12 месяцев за {t12.price_usdt:.2f} USDT

        Нажми на нужную команду — я создам счёт в CryptoBot и отправлю ссылку.
        После оплаты я автоматически проверю платёж
        и активирую Premium на выбранный срок.
        """
    )


def render_subscription_invoice(plan_key: str, pay_url: str) -> str:
    tariff = SUBSCRIPTION_TARIFFS[plan_key]
    months = tariff.months
    price = tariff.price_usdt
    limit = tariff.daily_limit

    return _wrap(
        f"""
        💎 Premium — оформление

        План: {months} мес. • {price:.2f} USDT
        Дневной лимит после активации: {limit} запросов.

        🔗 Ссылка на оплату в CryptoBot:
        {pay_url}

        После оплаты вернись в бот — я сам периодически проверяю статус
        и автоматически активирую подписку, как только платёж подтвердится.
        """
    )


def render_subscription_status(user: UserRecord) -> str:
    if is_premium(user):
        premium_until = getattr(user, "premium_until", None)
        limit = active_daily_limit(user)
        return _wrap(
            f"""
            💎 Статус подписки

            Подписка: Premium
            Действует до: {premium_until}
            Текущий лимит: {limit} запросов в день.

            Наслаждайся свободой. Если появятся вопросы — /help.
            """
        )

    limit = active_daily_limit(user)
    return _wrap(
        f"""
        💎 Статус подписки

        Сейчас у тебя Базовый план: {limit} запросов в день.

        Оформить Premium можно в разделе «{BTN_SUBSCRIPTION}»
        или командами:
        • /premium_1m
        • /premium_3m
        • /premium_12m
        """
    )


def render_referrals(user: UserRecord, bot_username: str) -> str:
    # Уникальный реф-код в хранилище
    ref_code = getattr(user, "ref_code", None)
    if not ref_code:
        ref_code = storage.ensure_ref_code(user)
    invited = getattr(user, "referrals_count", 0)
    bonus = getattr(user, "ref_bonus_limit", 0)
    limit = active_daily_limit(user)

    ref_link = f"https://t.me/{bot_username}?start={ref_code}"

    return _wrap(
        f"""
        👥 Рефералы

        Поделись ботом с людьми, которым он реально нужен.
        За каждого друга, который начнёт пользоваться ботом,
        ты получаешь дополнительные запросы в день.

        Твоя личная ссылка:
        {ref_link}

        Приглашено: {invited}
        Бонус к дневному лимиту: +{bonus}
        Итоговый лимит в день: {limit}

        Лучший реферал — тот, кому это действительно поможет.
        """
    )


def render_limit_exceeded(user: UserRecord) -> str:
    refresh_usage_if_needed(user)
    used = getattr(user, "used_today", 0)
    limit = active_daily_limit(user)

    return _wrap(
        f"""
        🚫 Лимит на сегодня исчерпан.

        Уже использовано: {used} / {limit} запросов.

        Что можно сделать:
        • Подождать до завтра — лимит обновится автоматически.
        • Оформить 💎 Premium, чтобы сильно расширить границы.
        • Зайти в «{BTN_REFERRALS}» и получить бонусы за приглашённых друзей.

        Если нужна помощь с выбором — загляни в раздел «{BTN_SUBSCRIPTION}».
        """
    )


def render_generic_error() -> str:
    return _wrap(
        """
        Что-то пошло не так на моей стороне.

        Я уже записал это в логи. Попробуй ещё раз чуть позже.
        Если ошибка повторяется — напиши моему создателю.
        """
    )


# ---------------------------------------------------------------------------
# LLM: стиль и обработка запросов
# ---------------------------------------------------------------------------

STYLE_HINT_BASE = _wrap(
    """
    Пиши чистым, аккуратным русским языком.
    Структурируй ответ по блокам, используй списки и **жирный** только там,
    где это действительно усиливает смысл.
    Эмодзи можно использовать, но умеренно — как акценты, а не как шум.
    Общий тон: умный, спокойный, по-братски, но без панибратства и пошлости.
    """
)


async def answer_with_typing(message: Message, text: str) -> None:
    """
    Лёгкая имитация "живого" ответа: показываем typing, пока формируем текст.
    Саму стриминговую генерацию пока не включаем, чтобы не ломать стабильность.
    """
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await message.answer(text)


# ---------------------------------------------------------------------------
# Handlers: команды и таскбар
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user, created = ensure_user(message)
    bot: Bot = message.bot
    me = await bot.get_me()
    text = render_onboarding(user, me.username or "BlackBoxGPT_bot")
    await message.answer(text, reply_markup=build_main_keyboard(), parse_mode=ParseMode.MARKDOWN)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    user, _ = ensure_user(message)
    bot: Bot = message.bot
    me = await bot.get_me()

    text = _wrap(
        f"""
        🧾 Справка

        Я — BlackBox GPT, универсальный ИИ-ассистент.

        Как со мной работать:
        • Просто пиши сообщения — я отвечаю в рамках твоего режима.
        • Снизу таскбар: «{BTN_MODES}», «{BTN_PROFILE}», «{BTN_SUBSCRIPTION}», «{BTN_REFERRALS}».
        • Режимы переключаются командами /mode_...

        Если потеряешься — всегда можно снова набрать /start.

        Твой бот: @{me.username}
        """
    )
    await message.answer(text, reply_markup=build_main_keyboard(), parse_mode=ParseMode.MARKDOWN)


# --- таскбар ---


@router.message(F.text == BTN_PROFILE)
async def on_profile(message: Message) -> None:
    user, _ = ensure_user(message)
    await message.answer(render_profile(user), parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == BTN_MODES)
async def on_modes(message: Message) -> None:
    user, _ = ensure_user(message)
    await message.answer(render_modes(user), parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == BTN_SUBSCRIPTION)
async def on_subscription(message: Message) -> None:
    await message.answer(render_subscription_overview(), parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == BTN_REFERRALS)
async def on_referrals(message: Message) -> None:
    user, _ = ensure_user(message)
    bot: Bot = message.bot
    me = await bot.get_me()
    await message.answer(
        render_referrals(user, me.username or "BlackBoxGPT_bot"),
        parse_mode=ParseMode.MARKDOWN,
    )


# --- смена режимов ---


@router.message(Command("mode_universal"))
async def cmd_mode_universal(message: Message) -> None:
    user, _ = ensure_user(message)
    set_mode(user, "universal")
    await message.answer(
        _wrap(
            """
            🧠 Режим переключён на: Универсальный.

            Можно задавать любые вопросы: от жизни и быта до сложного кода.
            """
        )
    )


@router.message(Command("mode_med"))
async def cmd_mode_med(message: Message) -> None:
    user, _ = ensure_user(message)
    set_mode(user, "med")
    await message.answer(
        _wrap(
            """
            🩺 Режим переключён на: Медицина.

            Обсуждаем здоровье, анализы, протоколы лечения — в формате
            объяснений и подсказок, но без постановки диагнозов
            и назначения конкретных лекарств.
            """
        )
    )


@router.message(Command("mode_mentor"))
async def cmd_mode_mentor(message: Message) -> None:
    user, _ = ensure_user(message)
    set_mode(user, "mentor")
    await message.answer(
        _wrap(
            """
            🔥 Режим переключён на: Наставник.

            Работаем с целями, фокусом, дисциплиной и внутренним стержнем.
            """
        )
    )


@router.message(Command("mode_business"))
async def cmd_mode_business(message: Message) -> None:
    user, _ = ensure_user(message)
    set_mode(user, "business")
    await message.answer(
        _wrap(
            """
            💼 Режим переключён на: Бизнес.

            Разбор идей, стратегии, воронки, продукты и деньги.
            """
        )
    )


@router.message(Command("mode_creative"))
async def cmd_mode_creative(message: Message) -> None:
    user, _ = ensure_user(message)
    set_mode(user, "creative")
    await message.answer(
        _wrap(
            """
            🎨 Режим переключён на: Креатив.

            Генерируем идеи, визуальные концепты, тексты и всё,
            что требует нестандартного мышления.
            """
        )
    )


# --- подписка / CryptoBot ---


async def _handle_premium_command(message: Message, plan_key: str) -> None:
    user, _ = ensure_user(message)

    try:
        invoice = await create_cryptobot_invoice(user.user_id, plan_key)
    except Exception as e:
        logger.exception("Failed to create CryptoBot invoice: %s", e)
        await message.answer(render_generic_error())
        return

    if not invoice or "pay_url" not in invoice:
        await message.answer(render_generic_error())
        return

    pay_url = invoice["pay_url"]
    storage.register_invoice(user.user_id, plan_key, invoice)  # функция уже есть в storage
    await message.answer(
        render_subscription_invoice(plan_key, pay_url),
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(Command("premium_1m"))
async def cmd_premium_1m(message: Message) -> None:
    await _handle_premium_command(message, "premium_1m")


@router.message(Command("premium_3m"))
async def cmd_premium_3m(message: Message) -> None:
    await _handle_premium_command(message, "premium_3m")


@router.message(Command("premium_12m"))
async def cmd_premium_12m(message: Message) -> None:
    await _handle_premium_command(message, "premium_12m")


@router.message(Command("premium_status"))
async def cmd_premium_status(message: Message) -> None:
    user, _ = ensure_user(message)

    # Если уже Premium — просто показываем статус
    if is_premium(user):
        await message.answer(render_subscription_status(user), parse_mode=ParseMode.MARKDOWN)
        return

    # Иначе — пробуем проверить последний счёт
    inv = storage.get_last_invoice(user.user_id)
    if not inv:
        await message.answer(render_subscription_status(user), parse_mode=ParseMode.MARKDOWN)
        return

    try:
        status = await get_invoice_status(inv.invoice_id)
    except Exception as e:
        logger.exception("Failed to check CryptoBot invoice: %s", e)
        await message.answer(render_generic_error())
        return

    if status == "paid":
        # Активируем подписку в сторидже
        storage.activate_subscription(user, inv.plan_key)
        user, _ = ensure_user(message)  # перечитываем обновлённого
        await message.answer(
            _wrap(
                """
                💎 Подписка успешно активирована.

                Premium включён, лимиты расширены.
                Добро пожаловать в взрослый режим работы с ИИ.
                """
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await message.answer(
            _wrap(
                f"""
                💎 Подписка пока не активирована.

                Текущий статус последнего счёта: {status!r}.

                Если ты уже оплатил — подожди пару минут и повтори /premium_status.
                Если статус долго не меняется — напиши создателю бота.
                """
            ),
            parse_mode=ParseMode.MARKDOWN,
        )


# ---------------------------------------------------------------------------
# Основной обработчик сообщений (диалог)
# ---------------------------------------------------------------------------

@router.message(F.text & ~F.text.startswith("/"))
async def on_user_message(message: Message) -> None:
    user, _ = ensure_user(message)

    # Проверка лимитов только для непремиумных (или по общему правилу)
    if remaining_requests(user) <= 0:
        await message.answer(render_limit_exceeded(user), parse_mode=ParseMode.MARKDOWN)
        return

    text = message.text or ""
    mode_key = getattr(user, "mode_key", DEFAULT_MODE)
    mode_cfg = ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES[DEFAULT_MODE])

    system_prompt = mode_cfg["system_prompt"]
    style_hint = STYLE_HINT_BASE

    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        reply = await generate_answer(
            prompt=text,
            system_prompt=system_prompt,
            mode_key=mode_key,
            style_hint=style_hint,
        )
    except Exception as e:
        logger.exception("LLM error: %s", e)
        await message.answer(render_generic_error())
        return

    register_usage(user)
    await message.answer(reply, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Запуск бота
# ---------------------------------------------------------------------------

async def main() -> None:
    bot = Bot(BOT_TOKEN, parse_mode=ParseMode.MARKDOWN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
