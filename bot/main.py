from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Crypto Pay
CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")

# База
DB_PATH = os.getenv("SUBSCRIPTION_DB_PATH", "subscription.db")

# Лимит бесплатных сообщений
FREE_MESSAGES_LIMIT = int(os.getenv("FREE_MESSAGES_LIMIT", "20"))

# Админы (username без @, через запятую / пробел)
ADMIN_USERNAMES = {
    u.strip().lower()
    for u in os.getenv("ADMIN_USERNAMES", "").replace(",", " ").split()
    if u.strip()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment (.env)")

LLM_AVAILABLE = bool(DEEPSEEK_API_KEY or GROQ_API_KEY)
CRYPTO_ENABLED = bool(CRYPTO_PAY_API_TOKEN)

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-70b-versatile")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Тарифы
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    code: str
    title: str
    months: int
    price_usdt: float
    description: str


PLANS: dict[str, Plan] = {
    "1m": Plan(
        code="1m",
        title="1 месяц доступа",
        months=1,
        price_usdt=5.0,
        description="Стартовый доступ к BlackBox GPT на 1 месяц",
    ),
    "3m": Plan(
        code="3m",
        title="3 месяца доступа",
        months=3,
        price_usdt=12.0,
        description="Оптимальный пакет на 3 месяца со скидкой",
    ),
    "12m": Plan(
        code="12m",
        title="12 месяцев доступа",
        months=12,
        price_usdt=60.0,
        description="Годовой доступ с максимальной выгодой",
    ),
}


# ---------------------------------------------------------------------------
# База данных (users_v2)
# ---------------------------------------------------------------------------


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаём необходимые таблицы."""
    with _get_conn() as conn:
        cur = conn.cursor()

        # Пользователи
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users_v2 (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id      INTEGER UNIQUE NOT NULL,
                username         TEXT,
                first_name       TEXT,
                last_name        TEXT,
                is_premium       INTEGER NOT NULL DEFAULT 0,
                premium_until_ts INTEGER,
                free_used        INTEGER NOT NULL DEFAULT 0,
                created_at_ts    INTEGER NOT NULL,
                updated_at_ts    INTEGER NOT NULL
            )
            """
        )

        conn.commit()


def get_or_create_user(
    telegram_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
) -> sqlite3.Row:
    now = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                """
                UPDATE users_v2
                SET username = ?, first_name = ?, last_name = ?, updated_at_ts = ?
                WHERE telegram_id = ?
                """,
                (username, first_name, last_name, now, telegram_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO users_v2 (
                    telegram_id,
                    username,
                    first_name,
                    last_name,
                    is_premium,
                    premium_until_ts,
                    free_used,
                    created_at_ts,
                    updated_at_ts
                )
                VALUES (?, ?, ?, ?, 0, NULL, 0, ?, ?)
                """,
                (telegram_id, username, first_name, last_name, now, now),
            )
        conn.commit()

        cur.execute(
            "SELECT * FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        return cur.fetchone()


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM users_v2 WHERE lower(username) = ?",
            (username.lower(),),
        )
        return cur.fetchone()


def user_is_premium(user_row: sqlite3.Row) -> bool:
    until_ts = user_row["premium_until_ts"]
    if not until_ts:
        return False
    try:
        return int(until_ts) > int(time.time())
    except (TypeError, ValueError):
        return False


def grant_premium(telegram_id: int, months: int) -> None:
    """Выдаём / продлеваем премиум на указанное количество месяцев."""
    extend_seconds = int(months * 30.4375 * 24 * 3600)  # ~месяц
    now = int(time.time())

    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT premium_until_ts FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        current_until = int(row["premium_until_ts"]) if row and row["premium_until_ts"] else 0
        base = current_until if current_until > now else now
        new_until = base + extend_seconds

        cur.execute(
            """
            UPDATE users_v2
            SET is_premium = 1,
                premium_until_ts = ?,
                updated_at_ts = ?
            WHERE telegram_id = ?
            """,
            (new_until, now, telegram_id),
        )
        conn.commit()


def get_free_used(telegram_id: int) -> int:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT free_used FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        if not row or row["free_used"] is None:
            return 0
        return int(row["free_used"])


def increment_free_used(telegram_id: int, delta: int = 1) -> int:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users_v2 SET free_used = free_used + ? WHERE telegram_id = ?",
            (delta, telegram_id),
        )
        conn.commit()
        cur.execute(
            "SELECT free_used FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        if not row or row["free_used"] is None:
            return delta
        return int(row["free_used"])


def is_user_admin(username: Optional[str]) -> bool:
    return bool(username and username.lower() in ADMIN_USERNAMES)


# ---------------------------------------------------------------------------
# LLM (DeepSeek / Groq)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Ты — BlackBox GPT, универсальный ИИ-ассистент в Telegram. "
    "Отвечай чётко, по делу, структурировано и дружелюбно. "
    "Избегай лишней воды, давай максимум пользы. "
    "Если вопрос касается здоровья, медицины, диагнозов или лечения — "
    "давай только общую справочную информацию и обязательно советуй "
    "обратиться к врачу. "
    "Всегда отвечай на русском языке, если явно не просят другой язык."
)


async def _call_deepseek(user_text: str) -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.7,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(DEEPSEEK_API_URL, headers=headers, json=payload)

    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"DeepSeek API вернул пустой ответ: {data}")
    return choices[0]["message"]["content"].strip()


async def _call_groq(user_text: str) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.7,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(GROQ_API_URL, headers=headers, json=payload)

    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Groq API вернул пустой ответ: {data}")
    return choices[0]["message"]["content"].strip()


async def generate_ai_reply(user_text: str) -> str:
    """
    Универсальный генератор ответа: DeepSeek → Groq → fallback.
    """
    last_error: Optional[Exception] = None

    if DEEPSEEK_API_KEY:
        try:
            return await _call_deepseek(user_text)
        except Exception as e:
            last_error = e
            logger.exception("DeepSeek API error: %r", e)

    if GROQ_API_KEY:
        try:
            return await _call_groq(user_text)
        except Exception as e:
            last_error = e
            logger.exception("Groq API error: %r", e)

    if last_error:
        return (
            "⚠️ Произошла внутренняя ошибка при обращении к ИИ.\n"
            "Попробуй повторить запрос немного позже."
        )

    return (
        "⚠️ ИИ-модель сейчас не настроена.\n"
        "Проверь конфигурацию бота или свяжись с администратором."
    )


# ---------------------------------------------------------------------------
# Crypto Pay (создание инвойса)
# ---------------------------------------------------------------------------


async def crypto_create_invoice(plan: Plan, telegram_id: int) -> dict:
    """
    Создаёт инвойс в Crypto Pay API для указанного тарифа.
    Возвращает объект Invoice из result.
    """
    if not CRYPTO_ENABLED:
        raise RuntimeError("Crypto Pay API is not configured")

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
        "Content-Type": "application/json",
    }

    payload_obj = {
        "telegram_id": telegram_id,
        "plan": plan.code,
        "created_at": int(time.time()),
    }

    data = {
        "currency_type": "crypto",
        "asset": "USDT",  # фиксируем оплату в USDT
        "amount": f"{plan.price_usdt:.2f}",
        "description": f"Подписка {plan.title} для BlackBox GPT",
        "payload": json.dumps(payload_obj),
        "allow_comments": False,
        "allow_anonymous": True,
        "expires_in": 3600,
    }

    async with httpx.AsyncClient(base_url=CRYPTO_PAY_API_URL, timeout=15) as client:
        resp = await client.post("/createInvoice", headers=headers, json=data)

    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise RuntimeError(f"Crypto Pay API error: {body}")
    invoice = body.get("result") or {}
    return invoice


# ---------------------------------------------------------------------------
# Доступ / лимиты
# ---------------------------------------------------------------------------


async def _ensure_user(message: Message) -> sqlite3.Row:
    from_user = message.from_user
    return get_or_create_user(
        telegram_id=from_user.id,
        username=from_user.username,
        first_name=from_user.first_name,
        last_name=from_user.last_name,
    )


async def _check_access(message: Message) -> Tuple[bool, sqlite3.Row]:
    """
    Возвращает (allowed, user_row).
    Если allowed == False — лимит бесплатных сообщений исчерпан.
    """
    user = await _ensure_user(message)
    username = message.from_user.username

    # Админы — всегда без ограничений
    if is_user_admin(username):
        return True, user

    # Премиум — без ограничений
    if user_is_premium(user):
        return True, user

    # Если нет подключённой модели — лимит не считаем
    if not LLM_AVAILABLE:
        return True, user

    used = int(user["free_used"] or 0)
    if used >= FREE_MESSAGES_LIMIT:
        return False, user

    new_used = increment_free_used(user["telegram_id"])
    logger.info(
        "User %s used free message #%s / %s",
        user["telegram_id"],
        new_used,
        FREE_MESSAGES_LIMIT,
    )
    return True, user


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="💬 Начать диалог"),
                KeyboardButton(text="⚡ Подписка"),
            ],
        ],
        resize_keyboard=True,
    )


def subscription_plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="1 месяц — 5 USDT",
                    callback_data="plan:1m",
                )
            ],
            [
                InlineKeyboardButton(
                    text="3 месяца — 12 USDT",
                    callback_data="plan:3m",
                )
            ],
            [
                InlineKeyboardButton(
                    text="12 месяцев — 60 USDT",
                    callback_data="plan:12m",
                )
            ],
        ]
    )


MENU_TEXTS = {"💬 Начать диалог", "⚡ Подписка"}


# ---------------------------------------------------------------------------
# Router & handlers
# ---------------------------------------------------------------------------

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await _ensure_user(message)

    text = (
        "<b>BlackBox GPT — Universal AI Assistant</b>\n\n"
        "Твой личный универсальный ИИ в Telegram.\n"
        "Помогаю с идеями, текстами, кодом, стратегией, решениями и разбором ситуаций.\n\n"
        f"Сейчас у тебя есть <b>{FREE_MESSAGES_LIMIT} бесплатных сообщений</b>, "
        "после — можно оформить премиум-подписку через USDT.\n\n"
        "Выбери действие на клавиатуре ниже 👇"
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "❓ <b>Что умеет BlackBox GPT</b>\n\n"
        "• Отвечать на вопросы по любой теме\n"
        "• Помогать с текстами, сценариями, структурой мыслей\n"
        "• Поддерживать в рабочих и личных задачах\n"
        "• Подсказывать по коду и технологиям\n\n"
        "Команды:\n"
        "/start — главное меню\n"
        "/subscription — оформить подписку\n"
        "/help — эта справка\n"
    )
    await message.answer(text)


async def _send_subscription_menu(message: Message) -> None:
    text = (
        "⚡ <b>Подписка BlackBox GPT Premium</b>\n\n"
        "Бесплатный лимит — "
        f"<b>{FREE_MESSAGES_LIMIT} сообщений</b>. После — безлимитный доступ по подписке.\n\n"
        "Тарифы:\n"
        "• 1 месяц — 5 USDT\n"
        "• 3 месяца — 12 USDT\n"
        "• 12 месяцев — 60 USDT\n\n"
        "Выбери нужный план 👇"
    )
    await message.answer(text, reply_markup=subscription_plans_keyboard())


@router.message(Command("subscription"))
async def cmd_subscription(message: Message) -> None:
    await _send_subscription_menu(message)


@router.message(F.text == "⚡ Подписка")
async def subscription_button(message: Message) -> None:
    await _send_subscription_menu(message)


@router.callback_query(F.data.startswith("plan:"))
async def subscription_plan_selected(callback: CallbackQuery) -> None:
    plan_code = callback.data.split(":", 1)[1]
    plan = PLANS.get(plan_code)
    if not plan:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return

    if not CRYPTO_ENABLED:
        await callback.answer(
            "Платёжный модуль ещё не настроен. Свяжись с админом.",
            show_alert=True,
        )
        return

    try:
        invoice = await crypto_create_invoice(plan, callback.from_user.id)
    except Exception as e:
        logger.exception("Error while creating invoice: %r", e)
        await callback.answer(
            "Ошибка при создании счёта. Попробуй ещё раз чуть позже.",
            show_alert=True,
        )
        return

    pay_url = (
        invoice.get("bot_invoice_url")
        or invoice.get("pay_url")
        or invoice.get("web_app_invoice_url")
        or invoice.get("mini_app_invoice_url")
    )

    if not pay_url:
        logger.error("Invoice without pay url: %r", invoice)
        await callback.answer(
            "Не удалось получить ссылку на оплату. Попробуй позже.",
            show_alert=True,
        )
        return

    text = (
        "💳 <b>Оформление подписки BlackBox GPT</b>\n\n"
        f"План: <b>{plan.title}</b>\n"
        f"Сумма: <b>{plan.price_usdt} USDT</b>\n\n"
        "Нажми кнопку ниже, чтобы перейти к оплате через <b>Crypto Bot</b>.\n\n"
        "После успешной оплаты свяжись с админом, чтобы он активировал премиум-доступ "
        "или подключи автоактивацию через Crypto Pay Webhook в коде."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Оплатить через Crypto Bot",
                    url=pay_url,
                )
            ],
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.message(F.text == "💬 Начать диалог")
async def start_dialog(message: Message) -> None:
    await message.answer(
        "Окей, я с тобой. Напиши, чем тебе помочь прямо сейчас 👇",
    )


@router.message(Command("grant_premium"))
async def cmd_grant_premium(message: Message) -> None:
    """
    /grant_premium <telegram_id|@username> <месяцев>
    Доступно только админам (ADMIN_USERNAMES).
    """
    if not is_user_admin(message.from_user.username):
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(
            "Использование:\n"
            "/grant_premium <telegram_id или @username> <месяцев>\n\n"
            "Примеры:\n"
            "/grant_premium 123456789 1\n"
            "/grant_premium @nickname 3"
        )
        return

    target = parts[1]
    try:
        months = int(parts[2])
    except ValueError:
        await message.reply("Количество месяцев должно быть целым числом.")
        return

    if target.startswith("@"):
        user_row = get_user_by_username(target[1:])
        if not user_row:
            await message.reply("Пользователь с таким username не найден в базе.")
            return
        telegram_id = int(user_row["telegram_id"])
    else:
        try:
            telegram_id = int(target)
        except ValueError:
            await message.reply("Некорректный telegram_id.")
            return

    grant_premium(telegram_id, months)
    await message.reply(
        f"Премиум на <b>{months}</b> мес. выдан пользователю "
        f"<code>{telegram_id}</code>."
    )


@router.message(F.chat.type == ChatType.PRIVATE)
async def handle_private_chat(message: Message) -> None:
    """
    Общий хэндлер для диалога с ИИ.
    """
    if not message.text:
        return

    # Спец-кнопки обрабатываются отдельными хэндлерами
    if message.text in MENU_TEXTS:
        return

    # Команды — отдельные хэндлеры
    if message.text.startswith("/"):
        return

    allowed, user_row = await _check_access(message)
    if not allowed:
        used = get_free_used(message.from_user.id)
        text = (
            "🕳 <b>Лимит бесплатных сообщений исчерпан</b>\n\n"
            f"Ты уже использовал <b>{used} / {FREE_MESSAGES_LIMIT}</b>.\n\n"
            "Чтобы продолжить общение без ограничений, оформи премиум-подписку 👇"
        )
        await message.answer(text, reply_markup=subscription_plans_keyboard())
        return

    reply = await generate_ai_reply(message.text)
    await message.answer(reply)


# ---------------------------------------------------------------------------
# Запуск бота
# ---------------------------------------------------------------------------


async def set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="help", description="Что умеет бот"),
        BotCommand(command="subscription", description="Оформить подписку"),
    ]
    await bot.set_my_commands(commands)


async def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)

    logger.info("Initializing database…")
    init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    await set_bot_commands(bot)

    logger.info("Starting BlackBox GPT bot polling…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
