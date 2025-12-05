from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

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
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")

ADMIN_USERNAMES = {
    u.strip().lower()
    for u in os.getenv("ADMIN_USERNAMES", "").replace(",", " ").split()
    if u.strip()
}

FREE_MESSAGES_LIMIT = int(os.getenv("FREE_MESSAGES_LIMIT", "20"))

DB_PATH = os.getenv("SUBSCRIPTION_DB_PATH", "subscription.db")


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")

LLM_AVAILABLE = bool(DEEPSEEK_API_KEY or GROQ_API_KEY)


# ---------------------------------------------------------------------------
# Database helpers (v2 schema, изолировано от старых таблиц)
# ---------------------------------------------------------------------------


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаём таблицы, если их ещё нет (v2-схема)."""
    with _get_conn() as conn:
        cur = conn.cursor()

        # Users table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users_v2 (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id       INTEGER UNIQUE NOT NULL,
                username          TEXT,
                first_name        TEXT,
                last_name         TEXT,
                is_premium        INTEGER NOT NULL DEFAULT 0,
                premium_until_ts  INTEGER,
                free_used         INTEGER NOT NULL DEFAULT 0,
                created_at_ts     INTEGER NOT NULL,
                updated_at_ts     INTEGER NOT NULL
            )
            """
        )

        # Invoices table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invoices_v2 (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id       TEXT UNIQUE NOT NULL,
                telegram_id      INTEGER NOT NULL,
                plan_code        TEXT NOT NULL,
                asset            TEXT NOT NULL,
                amount           REAL NOT NULL,
                status           TEXT NOT NULL,
                created_at_ts    INTEGER NOT NULL,
                paid_at_ts       INTEGER,
                raw_json         TEXT
            )
            """
        )

        # Projects table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS projects_v2 (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                title         TEXT NOT NULL,
                description   TEXT NOT NULL,
                updated_at_ts INTEGER NOT NULL
            )
            """
        )

        conn.commit()


def get_or_create_user_record(
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
                    telegram_id, username, first_name, last_name,
                    is_premium, premium_until_ts, free_used,
                    created_at_ts, updated_at_ts
                ) VALUES (?, ?, ?, ?, 0, NULL, 0, ?, ?)
                """,
                (telegram_id, username, first_name, last_name, now, now),
            )
        conn.commit()
        cur.execute(
            "SELECT * FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        return cur.fetchone()


def is_user_admin(username: Optional[str]) -> bool:
    return bool(username and username.lower() in ADMIN_USERNAMES)


def user_is_premium(row: sqlite3.Row) -> bool:
    ts = row["premium_until_ts"]
    return bool(ts and ts > int(time.time()))


def grant_premium(telegram_id: int, months: int) -> None:
    """Продлить / выдать премиум на N месяцев."""
    seconds = int(months * 30.4375 * 24 * 3600)  # ~месяцы
    now = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT premium_until_ts FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        current = row["premium_until_ts"] if row else None
        base = current if current and current > now else now
        new_until = base + seconds
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
        return int(row["free_used"]) if row else 0


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
        return int(row["free_used"]) if row else delta


def create_invoice_record(
    invoice_id: str,
    telegram_id: int,
    plan_code: str,
    asset: str,
    amount: float,
    status: str,
    raw_json: Dict[str, Any],
) -> None:
    now = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO invoices_v2 (
                invoice_id, telegram_id, plan_code, asset, amount,
                status, created_at_ts, paid_at_ts, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                invoice_id,
                telegram_id,
                plan_code,
                asset,
                amount,
                status,
                now,
                json.dumps(raw_json, ensure_ascii=False),
            ),
        )
        conn.commit()


def mark_invoice_paid(invoice_id: str) -> None:
    now = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE invoices_v2
            SET status = 'paid', paid_at_ts = ?
            WHERE invoice_id = ?
            """,
            (now, invoice_id),
        )
        conn.commit()


def get_user_projects(telegram_id: int) -> list[sqlite3.Row]:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM projects_v2
            WHERE telegram_id = ?
            ORDER BY updated_at_ts DESC
            """,
            (telegram_id,),
        )
        return cur.fetchall()


def upsert_project(telegram_id: int, title: str, description: str) -> None:
    now = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id FROM projects_v2
            WHERE telegram_id = ? AND title = ?
            """,
            (telegram_id, title),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                """
                UPDATE projects_v2
                SET description = ?, updated_at_ts = ?
                WHERE id = ?
                """,
                (description, now, row["id"]),
            )
        else:
            cur.execute(
                """
                INSERT INTO projects_v2 (telegram_id, title, description, updated_at_ts)
                VALUES (?, ?, ?, ?)
                """,
                (telegram_id, title, description, now),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Crypto Pay integration
# ---------------------------------------------------------------------------


class CryptoPayError(RuntimeError):
    pass


@dataclass(frozen=True)
class Plan:
    code: str
    title: str
    months: int
    price_usdt: float


PLANS: Dict[str, Plan] = {
    "month":   Plan(code="month",   title="1 месяц",      months=1,  price_usdt=5.0),
    "quarter": Plan(code="quarter", title="3 месяца",     months=3,  price_usdt=12.0),
    "year":    Plan(code="year",    title="12 месяцев",   months=12, price_usdt=60.0),
}


async def create_crypto_invoice(telegram_id: int, plan_code: str) -> Dict[str, Any]:
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN is not configured in .env")

    plan = PLANS.get(plan_code)
    if not plan:
        raise CryptoPayError(f"Unknown plan: {plan_code}")

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
        "Content-Type": "application/json",
    }

    payload = {
        "asset": "USDT",
        "amount": str(plan.price_usdt),
        "description": f"Подписка AI Medicine Premium — {plan.title}",
        "hidden_message": "Спасибо за поддержку AI Medicine Bot 💜",
        "payload": json.dumps(
            {"telegram_id": telegram_id, "plan": plan_code},
            ensure_ascii=False,
        ),
        "allow_comments": True,
        "expires_in": 3600,  # 1 час
    }

    async with httpx.AsyncClient(base_url=CRYPTO_PAY_API_URL, timeout=20.0) as client:
        resp = await client.post("/createInvoice", json=payload)
    try:
        data = resp.json()
    except json.JSONDecodeError as e:  # network protection
        raise CryptoPayError(f"Invalid response from Crypto Pay: {e}") from e

    if not data.get("ok"):
        raise CryptoPayError(f"Crypto Pay error: {data!r}")

    invoice = data["result"]

    create_invoice_record(
        invoice_id=str(invoice["invoice_id"]),
        telegram_id=telegram_id,
        plan_code=plan_code,
        asset=str(invoice.get("asset", "USDT")),
        amount=float(invoice.get("amount", plan.price_usdt)),
        status=str(invoice.get("status", "active")),
        raw_json=invoice,
    )

    return invoice


async def get_invoice_status(invoice_id: str) -> Dict[str, Any]:
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN is not configured in .env")

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(base_url=CRYPTO_PAY_API_URL, timeout=20.0) as client:
        resp = await client.post("/getInvoices", json={"invoice_ids": [invoice_id]})
    data = resp.json()
    if not data.get("ok"):
        raise CryptoPayError(f"Crypto Pay error: {data!r}")

    invoices = data.get("result", [])
    if not invoices:
        raise CryptoPayError("Invoice not found")

    return invoices[0]


# ---------------------------------------------------------------------------
# LLM integration (DeepSeek / Groq)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Ты умный ассистент AI Medicine Bot. "
    "Отвечай кратко и по делу, используй дружелюбный тон. "
    "Ты можешь помогать с медицинскими вопросами, но не ставишь диагнозы "
    "и всегда рекомендуешь очную консультацию врача при серьёзных симптомах. "
    "Также ты разбираешься в технологиях, продуктивности и саморазвитии."
)


class LLMNotConfiguredError(RuntimeError):
    pass


async def ask_llm(text: str, user_id: int, username: Optional[str]) -> str:
    if not LLM_AVAILABLE:
        raise LLMNotConfiguredError(
            "LLM API keys are not configured. "
            "Set DEEPSEEK_API_KEY or GROQ_API_KEY in your .env"
        )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": text,
        },
    ]

    if DEEPSEEK_API_KEY:
        url = "https://api.deepseek.com/chat/completions"
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "deepseek-chat",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1024,
            "stream": False,
            "user": str(user_id),
        }
    elif GROQ_API_KEY:
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "llama-3.1-70b-versatile",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1024,
            "stream": False,
            "user": str(user_id),
        }
    else:  # на всякий случай
        raise LLMNotConfiguredError("No LLM keys configured")

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logging.error("Unexpected LLM response: %r", data)
        raise RuntimeError(f"Unexpected LLM response: {e}") from e


# ---------------------------------------------------------------------------
# Bot & handlers
# ---------------------------------------------------------------------------

router = Router(name="main-router")


def _main_keyboard(is_admin: bool) -> ReplyKeyboardMarkup:
    buttons_row1 = [
        KeyboardButton(text="💡 Новый запрос"),
        KeyboardButton(text="💎 Подписка"),
    ]
    buttons_row2 = [KeyboardButton(text="📂 Проекты")]
    if is_admin:
        buttons_row2.append(KeyboardButton(text="⚙️ Админ"))

    return ReplyKeyboardMarkup(
        keyboard=[buttons_row1, buttons_row2],
        resize_keyboard=True,
        input_field_placeholder="Просто напиши свой вопрос…",
    )


async def _ensure_user(message: Message) -> sqlite3.Row:
    user = message.from_user
    assert user is not None
    return get_or_create_user_record(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )


async def _check_access_and_increment(message: Message) -> tuple[bool, str]:
    user = await _ensure_user(message)
    if user_is_premium(user):
        return True, "premium"

    used = get_free_used(message.from_user.id)
    if used >= FREE_MESSAGES_LIMIT:
        return False, "limit"

    new_used = increment_free_used(message.from_user.id, 1)
    logging.info(
        "User %s used free message #%s / %s",
        message.from_user.id,
        new_used,
        FREE_MESSAGES_LIMIT,
    )
    return True, "free"


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = await _ensure_user(message)
    username = message.from_user.full_name if message.from_user else "друг"
    is_admin = is_user_admin(message.from_user.username if message.from_user else None)

    status = "👑 Администратор." if is_admin else "🧑‍⚕️ Пользователь."
    premium_part = ""
    if user_is_premium(user):
        premium_part = "\n\n💎 У тебя активна подписка <b>AI Medicine Premium</b>."

    text = (
        f"Привет, <b>{username}</b>!\n\n"
        "Это <b>AI Medicine Bot</b> — умный ассистент по медицине и не только.\n\n"
        f"Твой статус: {status}"
        f"{premium_part}\n\n"
        "Просто напиши вопрос или нажми «💡 Новый запрос»."
    )

    kb = _main_keyboard(is_admin=is_admin)

    await message.answer(text, reply_markup=kb)

    if not LLM_AVAILABLE:
        await message.answer(
            "⚠️ Ключи для LLM не настроены.\n"
            "Добавь в <code>.env</code> переменные "
            "<code>DEEPSEEK_API_KEY</code> или <code>GROQ_API_KEY</code>.",
        )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Я — AI Medicine Bot.\n\n"
        "• Отвечаю на вопросы по медицине, технологиям и продуктивности.\n"
        "• Бесплатные сообщения ограничены.\n"
        "• Для безлимита оформи подписку через команду /subscription.\n\n"
        "Просто напиши свой вопрос.",
    )


@router.message(Command("subscription"))
@router.message(F.text.lower() == "подписка")
@router.message(F.text.lower() == "💎 подписка")
async def cmd_subscription(message: Message) -> None:
    await _ensure_user(message)

    lines = [
        "💎 <b>Подписка AI Medicine Premium</b>\n",
        "• Безлимитные запросы к ИИ",
        "• Приоритетная обработка",
        "• Фокус на медицине, технологиях и продуктивности\n",
        "Выбери план подписки:",
    ]

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="1 месяц — 5 USDT",
                    callback_data="sub:month",
                )
            ],
            [
                InlineKeyboardButton(
                    text="3 месяца — 12 USDT",
                    callback_data="sub:quarter",
                )
            ],
            [
                InlineKeyboardButton(
                    text="12 месяцев — 60 USDT",
                    callback_data="sub:year",
                )
            ],
        ]
    )

    await message.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("sub:"))
async def cb_choose_plan(callback: CallbackQuery) -> None:
    plan_code = callback.data.split(":", 1)[1]
    await callback.answer()

    if not CRYPTO_PAY_API_TOKEN:
        await callback.message.answer(
            "⚠️ Крипто-оплата ещё не настроена.\n"
            "Добавь CRYPTO_PAY_API_TOKEN в .env для работы подписки.",
        )
        return

    plan = PLANS.get(plan_code)
    if not plan:
        await callback.message.answer("Не удалось определить выбранный план.")
        return

    wait_msg = await callback.message.answer("Создаю счёт на оплату, секунду…")

    try:
        invoice = await create_crypto_invoice(callback.from_user.id, plan_code)
    except Exception:  # noqa: BLE001
        logging.exception("Unexpected error while creating invoice")
        await wait_msg.edit_text(
            "🥺 Не удалось создать счёт из-за непредвиденной ошибки. "
            "Попробуй ещё раз позже.",
        )
        return

    pay_url = invoice.get("pay_url") or invoice.get("bot_link")
    if not pay_url:
        await wait_msg.edit_text(
            "🥺 Счёт создан, но не удалось получить ссылку на оплату. "
            "Попробуй ещё раз позже.",
        )
        return

    text = (
        f"💳 Счёт на оплату: <b>{plan.title}</b>\n\n"
        "Чтобы оплатить подписку, просто перейди по ссылке ниже 👇"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Оплатить подписку",
                    url=pay_url,
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил, проверить",
                    callback_data=f"check:{invoice['invoice_id']}:{plan_code}",
                )
            ],
        ]
    )

    await wait_msg.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("check:"))
async def cb_check_invoice(callback: CallbackQuery) -> None:
    _, invoice_id, plan_code = callback.data.split(":", 2)
    await callback.answer("Проверяю оплату…")

    try:
        invoice = await get_invoice_status(invoice_id)
    except Exception:  # noqa: BLE001
        logging.exception("Error while checking invoice")
        await callback.message.answer(
            "🥺 Не удалось проверить статус счёта. Попробуй чуть позже.",
        )
        return

    status = str(invoice.get("status", "")).lower()
    if status != "paid":
        await callback.message.answer(
            "Похоже, счёт ещё не оплачен.\n"
            "Если ты уже отправил перевод, подожди пару минут и нажми кнопку "
            "«✅ Я оплатил, проверить» ещё раз.",
        )
        return

    # фиксируем оплату и выдаём премиум
    mark_invoice_paid(invoice_id)
    plan = PLANS.get(plan_code)
    if plan:
        grant_premium(callback.from_user.id, months=plan.months)

    await callback.message.answer(
        "💜 Оплата успешно получена!\n\n"
        "Тебе активирована подписка <b>AI Medicine Premium</b>. "
        "Теперь можно задавать безлимитное количество вопросов.",
    )


@router.message(Command("projects"))
async def cmd_projects(message: Message) -> None:
    await _ensure_user(message)
    rows = get_user_projects(message.from_user.id)

    if not rows:
        await message.answer(
            "📂 У тебя пока нет сохранённых проектов.\n\n"
            "Отправь команду:\n"
            "<code>/project_new Название проекта — краткое описание</code>",
        )
        return

    lines = ["📂 <b>Твои проекты</b>:\n"]
    for idx, row in enumerate(rows, start=1):
        lines.append(f"{idx}. <b>{row['title']}</b>\n{row['description']}\n")

    await message.answer("\n".join(lines))


@router.message(Command("project_new"))
async def cmd_project_new(message: Message) -> None:
    await _ensure_user(message)
    parts = message.text.split(maxsplit=1) if message.text else []
    if len(parts) < 2:
        await message.answer(
            "Чтобы создать проект, используй формат:\n"
            "<code>/project_new Название проекта — краткое описание</code>",
        )
        return

    payload = parts[1].strip()
    if "—" in payload:
        title, description = [p.strip() for p in payload.split("—", 1)]
    elif "-" in payload:
        title, description = [p.strip() for p in payload.split("-", 1)]
    else:
        title, description = payload, "Описание пока не задано."

    if not title:
        await message.answer("Название проекта не может быть пустым.")
        return

    upsert_project(message.from_user.id, title=title, description=description)

    await message.answer(
        f"✅ Проект <b>{title}</b> сохранён.\n"
        "В будущем я смогу подстраивать ответы под контекст этого проекта.",
    )


@router.message(Command("new"))
@router.message(F.text.startswith("💡 Новый запрос"))
async def cmd_new(message: Message) -> None:
    # В этой версии контекст диалога не сохраняется,
    # просто даём пользователю подсказку.
    await message.answer("Диалог обнулён. Напиши новый вопрос.")


@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.text,
    ~F.text.startswith("/"),
)
async def handle_chat(message: Message) -> None:
    ok, reason = await _check_access_and_increment(message)
    if not ok:
        await message.answer(
            "🤖 Бесплатный лимит сообщений исчерпан.\n\n"
            "Чтобы продолжить общение без ограничений, оформи подписку через "
            "команду /subscription.",
        )
        return

    try:
        reply = await ask_llm(
            text=message.text,
            user_id=message.from_user.id,
            username=message.from_user.username,
        )
    except LLMNotConfiguredError:
        await message.answer(
            "⚠️ Ключи для LLM не настроены.\n"
            "Добавь в <code>.env</code> переменные "
            "<code>DEEPSEEK_API_KEY</code> или <code>GROQ_API_KEY</code>.",
        )
        return
    except Exception:  # noqa: BLE001
        logging.exception("LLM error")
        await message.answer(
            "🥺 Что-то пошло не так при обращении к модели. "
            "Попробуй повторить запрос чуть позже.",
        )
        return

    await message.answer(reply)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def _set_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start",        description="Перезапуск и приветствие"),
        BotCommand(command="new",          description="Новый запрос"),
        BotCommand(command="subscription", description="Подписка AI Medicine"),
        BotCommand(command="projects",     description="Мои проекты"),
        BotCommand(command="help",         description="Что умеет бот"),
    ]
    await bot.set_my_commands(commands)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    logging.info("Starting AI Medicine bot…")

    init_db()

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    await _set_bot_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
