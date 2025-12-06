from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import textwrap
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ChatType, ParseMode
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
# Тарифы (только Crypto / USDT)
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
        price_usdt=7.99,
        description="Стартовый доступ к BlackBox GPT на 1 месяц",
    ),
    "3m": Plan(
        code="3m",
        title="3 месяца доступа",
        months=3,
        price_usdt=26.99,
        description="Удобный пакет на 3 месяца со скидкой",
    ),
    "12m": Plan(
        code="12m",
        title="12 месяцев доступа",
        months=12,
        price_usdt=82.99,
        description="Годовой доступ с максимальной выгодой",
    ),
}


# ---------------------------------------------------------------------------
# База данных (users_v2 + messages для истории)
# ---------------------------------------------------------------------------


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаём необходимые таблицы (пользователи + история сообщений)."""
    with _get_conn() as conn:
        cur = conn.cursor()

        # Пользователи
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_premium INTEGER NOT NULL DEFAULT 0,
                premium_until_ts INTEGER,
                free_used INTEGER NOT NULL DEFAULT 0,
                created_at_ts INTEGER NOT NULL,
                updated_at_ts INTEGER NOT NULL
            )
            """
        )

        # История сообщений (для адаптации под стиль общения)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                role TEXT NOT NULL,          -- 'user' или 'assistant'
                content TEXT NOT NULL,
                created_at_ts INTEGER NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_user_ts
            ON messages(telegram_id, created_at_ts)
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
    extend_seconds = int(months * 30.4375 * 24 * 3600)  # ~ месяц
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
            SET is_premium = 1, premium_until_ts = ?, updated_at_ts = ?
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
# История сообщений и адаптация стиля
# ---------------------------------------------------------------------------


def save_message(telegram_id: int, role: str, content: str) -> None:
    """Сохраняем сообщение пользователя / ассистента в БД."""
    content = (content or "").strip()
    if not content:
        return

    ts = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO messages (telegram_id, role, content, created_at_ts)
            VALUES (?, ?, ?, ?)
            """,
            (telegram_id, role, content, ts),
        )
        conn.commit()


def get_recent_user_messages(telegram_id: int, limit: int = 30) -> list[str]:
    """Получаем последние сообщения пользователя (для анализа стиля)."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT content FROM messages
            WHERE telegram_id = ? AND role = 'user'
            ORDER BY created_at_ts DESC
            LIMIT ?
            """,
            (telegram_id, limit),
        )
        rows = cur.fetchall()
    # Разворачиваем: старые → новые
    return [row["content"] for row in reversed(rows)]


def build_style_hint(telegram_id: int) -> str:
    """
    Строим подсказку для LLM по стилю ответа:
    - "ты" / "Вы"
    - объём (кратко / средне / развёрнуто)
    """
    messages = get_recent_user_messages(telegram_id, limit=30)
    if not messages:
        return ""

    all_text = " ".join(messages)
    lower = all_text.lower()

    # Примитивное определение формальности
    formal_markers = [
        "здравствуйте",
        "добрый день",
        "добрый вечер",
        "уважаем",
        "будьте добры",
    ]
    is_formal = any(m in lower for m in formal_markers) or " вы " in lower

    lengths = [len(m) for m in messages if m.strip()]
    avg_len = sum(lengths) / len(lengths) if lengths else 0

    if avg_len < 80:
        length_hint = (
            "Отвечай более кратко и структурировано — 3–6 ёмких абзацев "
            "или списком из 5–9 пунктов."
        )
    elif avg_len > 200:
        length_hint = (
            "Можно отвечать развёрнуто, но без воды: чёткая структура, "
            "подзаголовки и чёткий вывод в конце."
        )
    else:
        length_hint = (
            "Держи баланс между краткостью и глубиной: объясняй понятно, "
            "но не растекайся мыслью по древу."
        )

    if is_formal:
        tone_hint = (
            "Обращайся к пользователю на «Вы», стиль спокойный, деловой и уважительный."
        )
    else:
        tone_hint = (
            "Обращайся к пользователю на «ты», стиль живой, поддерживающий, "
            "но без панибратства."
        )

    return (
        "Адаптируй стиль ответов под пользователя. "
        f"{tone_hint} {length_hint}"
    )


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


async def _call_deepseek(user_text: str, style_hint: Optional[str]) -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")

    sys_prompt = SYSTEM_PROMPT
    if style_hint:
        sys_prompt = SYSTEM_PROMPT + "\n\n" + style_hint

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": sys_prompt},
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


async def _call_groq(user_text: str, style_hint: Optional[str]) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured")

    sys_prompt = SYSTEM_PROMPT
    if style_hint:
        sys_prompt = SYSTEM_PROMPT + "\n\n" + style_hint

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": sys_prompt},
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


async def generate_ai_reply(user_text: str, style_hint: Optional[str] = None) -> str:
    """
    Универсальный генератор ответа: DeepSeek → Groq → fallback.
    Учитывает style_hint для подстройки под пользователя.
    """
    last_error: Optional[Exception] = None

    if DEEPSEEK_API_KEY:
        try:
            return await _call_deepseek(user_text, style_hint)
        except Exception as e:
            last_error = e
            logger.exception("DeepSeek API error: %r", e)

    if GROQ_API_KEY:
        try:
            return await _call_groq(user_text, style_hint)
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
                KeyboardButton(text=" Начать диалог"),
                KeyboardButton(text="⚡ Подписка"),
            ],
        ],
        resize_keyboard=True,
    )


def subscription_plans_keyboard() -> InlineKeyboardMarkup:
    # Кнопки собираем из PLANS, чтобы цены всегда совпадали
    inline_rows = []
    for code in ("1m", "3m", "12m"):
        plan = PLANS[code]
        inline_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan.title} — {plan.price_usdt} USDT",
                    callback_data=f"plan:{code}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=inline_rows)


MENU_TEXTS = {" Начать диалог", "⚡ Подписка"}


# ---------------------------------------------------------------------------
# Живое печатание текста (stream через edit_message)
# ---------------------------------------------------------------------------

STREAM_CHUNK_SIZE = 80
STREAM_MAX_STEPS = 40
STREAM_DELAY_SECONDS = 0.12


def _chunk_text_for_streaming(text: str) -> list[str]:
    """Режем текст на куски для поэтапного редактирования сообщения."""
    text = text.strip()
    if not text:
        return []

    words = text.split()
    if not words:
        return [text]

    raw_chunks: list[str] = []
    current = ""

    for w in words:
        if len(current) + len(w) + (1 if current else 0) <= STREAM_CHUNK_SIZE:
            current = f"{current} {w}".strip()
        else:
            if current:
                raw_chunks.append(current)
            current = w

    if current:
        raw_chunks.append(current)

    # Ограничиваем количество шагов, чтобы не спамить Telegram обновлениями
    if len(raw_chunks) <= STREAM_MAX_STEPS:
        return raw_chunks

    step = math.ceil(len(raw_chunks) / STREAM_MAX_STEPS)
    chunks: list[str] = []
    for i in range(0, len(raw_chunks), step):
        chunks.append(" ".join(raw_chunks[i : i + step]))
    return chunks


async def stream_reply_text(message: Message, text: str) -> None:
    """
    Показываем ответ как «живое» печатание:
    - сначала отправляем сообщение,
    - потом по чуть-чуть дописываем текст через edit_message_text.
    """
    text = (text or "").strip()
    if not text:
        return

    chunks = _chunk_text_for_streaming(text)

    # Если текст короткий — не мучаемся, отправляем как есть
    if len(chunks) <= 1:
        await message.answer(text)
        return

    bot = message.bot

    # Первое сообщение
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    current = chunks[0]
    sent = await message.answer(current, parse_mode=None)

    for chunk in chunks[1:]:
        await asyncio.sleep(STREAM_DELAY_SECONDS)
        current = f"{current} {chunk}".strip()
        try:
            await bot.edit_message_text(
                current,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                parse_mode=None,  # без HTML, чтобы не ломать разметку при обрезке
            )
            await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        except Exception as e:
            logger.exception("Streaming edit error: %r", e)
            # Фоллбек: просто отправляем финальный текст отдельным сообщением
            if current != text:
                await message.answer(text, parse_mode=None)
            break


# ---------------------------------------------------------------------------
# Router & handlers
# ---------------------------------------------------------------------------

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user_row = await _ensure_user(message)
    is_premium = user_is_premium(user_row)

    name = message.from_user.first_name or "друг"

    premium_line = (
        "У тебя уже активирован Premium-доступ: можешь общаться без ограничений.\n\n"
        if is_premium
        else f"Сейчас у тебя есть {FREE_MESSAGES_LIMIT} бесплатных сообщений, "
        "после — можно оформить премиум-подписку через USDT.\n\n"
    )

    text = (
        f"Привет, {name} 👋\n\n"
        "Я — <b>BlackBox GPT — Universal AI Assistant</b>.\n\n"
        "Как со мной лучше всего работать:\n\n"
        "1️⃣ Нажми кнопку «Начать диалог» внизу.\n"
        "2️⃣ Пиши любые запросы: от стратегии и кода до медицины и личных вопросов.\n"
        "3️⃣ Я анализирую твою манеру общения и постепенно подстраиваюсь под твой стиль, "
        "чтобы диалог ощущался максимально живым.\n\n"
        + premium_line +
        "Если нужно больше мощности и свободы — загляни в раздел «⚡ Подписка».\n\n"
        "Готов? Просто нажми «Начать диалог» и напиши, чем тебе помочь прямо сейчас."
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
    lines = ["⚡ <b>Подписка BlackBox GPT Premium</b>\n"]
    if LLM_AVAILABLE:
        lines.append(
            f"Бесплатный лимит — {FREE_MESSAGES_LIMIT} сообщений.\n"
            "Дальше — безлимитный доступ по подписке.\n"
        )

    lines.append("\nТарифы:")

    for code in ("1m", "3m", "12m"):
        plan = PLANS[code]
        lines.append(f"• {plan.title} — {plan.price_usdt} USDT")

    lines.append("\nВыбери нужный план — я создам ссылку на оплату в Crypto Bot.")

    text = "\n".join(lines)
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
            "Платёжный модуль ещё не настроен.\nСвяжись с админом.",
            show_alert=True,
        )
        return

    try:
        invoice = await crypto_create_invoice(plan, callback.from_user.id)
    except Exception as e:
        logger.exception("Error while creating invoice: %r", e)
        await callback.answer(
            "Ошибка при создании счёта.\nПопробуй ещё раз чуть позже.",
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
            "Не удалось получить ссылку на оплату.\nПопробуй позже.",
            show_alert=True,
        )
        return

    text = (
        "✅ <b>Оформление подписки BlackBox GPT</b>\n\n"
        f"План: {plan.title}\n"
        f"Сумма: {plan.price_usdt} USDT\n\n"
        "Нажми кнопку ниже, чтобы перейти к оплате через Crypto Bot.\n\n"
        "После успешной оплаты можно реализовать автоактивацию премиума через "
        "webhook Crypto Pay или активировать доступ вручную командой админа."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить через Crypto Bot",
                    url=pay_url,
                )
            ]
        ]
    )

    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.message(F.text == " Начать диалог")
async def start_dialog(message: Message) -> None:
    await message.answer(
        "Окей, я с тобой. Напиши, чем тебе помочь прямо сейчас 🤝",
    )


@router.message(Command("grant_premium"))
async def cmd_grant_premium(message: Message) -> None:
    """
    /grant_premium <telegram_id | @username> <месяцев>
    Доступно только админам (ADMIN_USERNAMES).
    """
    if not is_user_admin(message.from_user.username):
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply(
            "Использование:\n"
            "/grant_premium <telegram_id | @username> <месяцев>\n\n"
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
        f"Премиум на {months} мес. выдан пользователю `{telegram_id}`.",
        parse_mode=ParseMode.MARKDOWN,
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

    allowed, _user_row = await _check_access(message)
    if not allowed:
        used = get_free_used(message.from_user.id)
        text = (
            "⚠️ Лимит бесплатных сообщений исчерпан.\n\n"
            f"Ты уже использовал {used} / {FREE_MESSAGES_LIMIT}.\n\n"
            "Чтобы продолжить общение без ограничений, оформи премиум-подписку."
        )
        await message.answer(text, reply_markup=subscription_plans_keyboard())
        return

    user_id = message.from_user.id

    # Сохраняем сообщение пользователя
    save_message(user_id, "user", message.text)

    # Подсказка по стилю на основе истории
    style_hint = build_style_hint(user_id)

    # Получаем ответ от ИИ
    reply = await generate_ai_reply(message.text, style_hint=style_hint)

    # Сохраняем ответ ассистента
    save_message(user_id, "assistant", reply)

    # Живое печатание ответа
    await stream_reply_text(message, reply)


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
