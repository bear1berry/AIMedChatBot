# BlackBox GPT Telegram Bot - main module
# --------------------------------------
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

from .payments_crypto import create_invoice, fetch_invoice_status

# ---------------------------------------------------------------------------
# Base config & logging
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR.parent / ".env"

if ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)
else:
    load_dotenv()  # fallback, если .env в корне проекта

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_TOKEN")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment")

if not DEEPSEEK_API_KEY:
    logging.warning(
        "DEEPSEEK_API_KEY is not set – бот будет отвечать заглушкой вместо модели."
    )

DB_PATH = BASE_DIR / "blackbox.sqlite3"

FREE_MESSAGES_LIMIT = 20
REFERRAL_BONUS_DAYS = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id       INTEGER PRIMARY KEY,
                username      TEXT,
                first_name    TEXT,
                last_name     TEXT,
                mode          TEXT DEFAULT 'universal',
                free_used     INTEGER DEFAULT 0,
                premium_until INTEGER,
                ref_code      TEXT,
                ref_by        INTEGER,
                created_at    INTEGER,
                updated_at    INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                invoice_id   INTEGER PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                plan_code    TEXT,
                amount       TEXT,
                asset        TEXT,
                status       TEXT,
                created_at   INTEGER,
                paid_at      INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id  INTEGER NOT NULL,
                referred_id  INTEGER NOT NULL UNIQUE,
                created_at   INTEGER,
                bonus_given  INTEGER DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_dossier (
                user_id    INTEGER PRIMARY KEY,
                text       TEXT,
                updated_at INTEGER
            )
            """
        )
        conn.commit()
        log.info("DB initialized at %s", DB_PATH)
    finally:
        conn.close()


@dataclass
class User:
    user_id: int
    username: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    mode: str
    free_used: int
    premium_until: Optional[int]
    ref_code: Optional[str]
    ref_by: Optional[int]
    created_at: int
    updated_at: int

    @property
    def is_premium(self) -> bool:
        if self.premium_until is None:
            return False
        return self.premium_until > int(time.time())


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        user_id=row["user_id"],
        username=row["username"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        mode=row["mode"],
        free_used=row["free_used"],
        premium_until=row["premium_until"],
        ref_code=row["ref_code"],
        ref_by=row["ref_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _register_referral(conn: sqlite3.Connection, referrer_id: int, referred_id: int) -> None:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM referrals WHERE referred_id = ?", (referred_id,))
    if cur.fetchone():
        return

    now = int(time.time())
    cur.execute(
        """
        INSERT INTO referrals (referrer_id, referred_id, created_at, bonus_given)
        VALUES (?, ?, ?, 0)
        """,
        (referrer_id, referred_id, now),
    )
    _add_premium_days(conn, referrer_id, REFERRAL_BONUS_DAYS)
    _add_premium_days(conn, referred_id, REFERRAL_BONUS_DAYS)
    cur.execute(
        "UPDATE referrals SET bonus_given = 1 WHERE referrer_id = ? AND referred_id = ?",
        (referrer_id, referred_id),
    )
    log.info(
        "Referral registered: referrer=%s, referred=%s, +%s day premium each",
        referrer_id,
        referred_id,
        REFERRAL_BONUS_DAYS,
    )


def _add_premium_days(conn: sqlite3.Connection, user_id: int, days: int) -> None:
    cur = conn.cursor()
    cur.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    now = int(time.time())
    if row and row["premium_until"]:
        base = max(row["premium_until"], now)
    else:
        base = now
    new_until = base + days * 86400
    cur.execute(
        "UPDATE users SET premium_until = ?, updated_at = ? WHERE user_id = ?",
        (new_until, now, user_id),
    )
    log.info("User %s premium_until set to %s", user_id, new_until)


def get_or_create_user(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    referrer_id: Optional[int] = None,
) -> User:
    now = int(time.time())
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if row:
            cur.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?, last_name = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (username, first_name, last_name, now, user_id),
            )
            conn.commit()
            cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            return _row_to_user(row)

        # новый пользователь
        ref_code = str(user_id)
        cur.execute(
            """
            INSERT INTO users (
                user_id, username, first_name, last_name, mode,
                free_used, premium_until, ref_code, ref_by,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'universal', 0, NULL, ?, NULL, ?, ?)
            """,
            (user_id, username, first_name, last_name, ref_code, now, now),
        )
        conn.commit()
        log.info("New user %s created", user_id)

        user = User(
            user_id=user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            mode="universal",
            free_used=0,
            premium_until=None,
            ref_code=ref_code,
            ref_by=None,
            created_at=now,
            updated_at=now,
        )

        # реферальная система
        if referrer_id and referrer_id != user_id:
            _register_referral(conn, referrer_id, user_id)
            conn.commit()

        return user
    finally:
        conn.close()


def get_user(user_id: int) -> Optional[User]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def increment_free_used(user_id: int) -> None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET free_used = free_used + 1, updated_at = ? WHERE user_id = ?",
            (int(time.time()), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_mode(user_id: int, mode: str) -> None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET mode = ?, updated_at = ? WHERE user_id = ?",
            (mode, int(time.time()), user_id),
        )
        conn.commit()
    finally:
        conn.close()


def save_invoice_record(
    invoice_id: int,
    user_id: int,
    plan_code: str,
    amount: str,
    asset: str,
    status: str,
) -> None:
    """
    Сохраняем / обновляем запись по инвойсу.
    Не затираем created_at при повторном сохранении.
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        now = int(time.time())
        cur.execute(
            """
            INSERT OR REPLACE INTO invoices
            (invoice_id, user_id, plan_code, amount, asset, status, created_at, paid_at)
            VALUES (
                ?, ?, ?, ?, ?, ?,
                COALESCE((SELECT created_at FROM invoices WHERE invoice_id = ?), ?),
                (SELECT paid_at FROM invoices WHERE invoice_id = ?)
            )
            """,
            (
                invoice_id,
                user_id,
                plan_code,
                amount,
                asset,
                status,
                invoice_id,
                now,
                invoice_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def mark_invoice_paid(invoice_id: int, paid_at: Optional[int] = None) -> None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        if paid_at is None:
            paid_at = int(time.time())
        cur.execute(
            "UPDATE invoices SET status = 'paid', paid_at = ? WHERE invoice_id = ?",
            (paid_at, invoice_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_invoice(user_id: int) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM invoices
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        return cur.fetchone()
    finally:
        conn.close()


def get_dossier(user_id: int) -> Optional[str]:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT text FROM user_dossier WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row["text"] if row else None
    finally:
        conn.close()


def save_dossier(user_id: int, text: str) -> None:
    conn = _get_conn()
    try:
        cur = conn.cursor()
        now = int(time.time())
        cur.execute(
            """
            INSERT INTO user_dossier (user_id, text, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE
                SET text = excluded.text,
                    updated_at = excluded.updated_at
            """,
            (user_id, text, now),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Assistant modes & UI
# ---------------------------------------------------------------------------

MODES: Dict[str, Dict[str, str]] = {
    "universal": {"icon": "🌙", "title": "Универсальный"},
    "deep": {"icon": "🧠", "title": "Глубокий разбор"},
    "focus": {"icon": "🎯", "title": "Фокус / Задачи"},
    "creative": {"icon": "🔥", "title": "Креатив / Идеи"},
    "mentor": {"icon": "🧿", "title": "Ментор / Мотивация"},
}

BTN_NEW = "💡 Новый запрос"
BTN_PREMIUM = "⚡ Премиум"
BTN_PROFILE = "👤 Профиль"
BTN_REFERRAL = "👥 Пригласить друга"
BTN_CHECK_PAYMENT = "🔁 Проверить оплату"


def _mode_button_text(code: str, active: str) -> str:
    cfg = MODES[code]
    base = f"{cfg['icon']} {cfg['title']}"
    if code == active:
        base += " ✓"
    return base


def build_main_keyboard(active_mode: str) -> ReplyKeyboardMarkup:
    row1 = [
        KeyboardButton(text=_mode_button_text("universal", active_mode)),
        KeyboardButton(text=_mode_button_text("deep", active_mode)),
    ]
    row2 = [
        KeyboardButton(text=_mode_button_text("focus", active_mode)),
        KeyboardButton(text=_mode_button_text("creative", active_mode)),
    ]
    row3 = [KeyboardButton(text=_mode_button_text("mentor", active_mode))]
    row4 = [
        KeyboardButton(text=BTN_NEW),
        KeyboardButton(text=BTN_PREMIUM),
        KeyboardButton(text=BTN_PROFILE),
    ]
    row5 = [
        KeyboardButton(text=BTN_REFERRAL),
        KeyboardButton(text=BTN_CHECK_PAYMENT),
    ]
    return ReplyKeyboardMarkup(
        keyboard=[row1, row2, row3, row4, row5],
        resize_keyboard=True,
        is_persistent=True,
    )


def detect_mode_from_text(text: str) -> Optional[str]:
    for code, cfg in MODES.items():
        if cfg["title"] in text:
            return code
    return None


def format_modes_hint(active_mode: str) -> str:
    lines = [
        "Можно в любой момент сменить режим — это влияет только на стиль и глубину ответов, а не ограничивает функционал.",
        "",
    ]
    for code, cfg in MODES.items():
        mark = "✓" if code == active_mode else ""
        lines.append(f"{cfg['icon']} <b>{cfg['title']}</b> {mark}")
    return "\n".join(lines)


def format_profile_text(user: User) -> str:
    parts: list[str] = []
    parts.append("<b>👤 Профиль BlackBox GPT</b>")
    parts.append("")
    name = user.first_name or ""
    if user.username:
        name = f"{name} @{user.username}".strip()
    if name:
        parts.append(f"Имя: {name}")
    parts.append(f"Режим ассистента: {MODES.get(user.mode, {}).get('title', user.mode)}")
    parts.append(
        f"Бесплатные сообщения: {min(user.free_used, FREE_MESSAGES_LIMIT)}/{FREE_MESSAGES_LIMIT}"
    )
    if user.is_premium:
        until = time.strftime(
            "%d.%m.%Y %H:%M", time.localtime(user.premium_until or 0)
        )
        parts.append(f"Статус: <b>Premium</b> до {until}")
    else:
        parts.append("Статус: Free (пока без подписки)")
    dossier = get_dossier(user.user_id)
    parts.append("")
    parts.append("<b>🧾 Личное досье</b>")
    if dossier:
        parts.append(dossier)
    else:
        parts.append("Я ещё собираю информацию о тебе по ходу общения.")
    return "\n".join(parts)


def format_premium_text(user: User) -> str:
    parts: list[str] = [
        "<b>⚡ Подписка BlackBox GPT Premium</b>",
        "",
        f"Бесплатный лимит — {FREE_MESSAGES_LIMIT} сообщений.",
        "После — безлимитный доступ по подписке.",
        "",
        "Тарифы:",
        "• 1 месяц — 5 USDT",
        "• 3 месяца — 12 USDT",
        "• 12 месяцев — 60 USDT",
        "",
        "Отправь одно из слов, чтобы создать счёт:",
        "<code>1m</code> — 1 месяц, <code>3m</code> — 3 месяца, <code>12m</code> — 12 месяцев.",
    ]
    if user.is_premium:
        until = time.strftime(
            "%d.%m.%Y %H:%M", time.localtime(user.premium_until or 0)
        )
        parts.append("")
        parts.append(f"У тебя уже активен <b>Premium</b> до {until}.")
    return "\n".join(parts)


def format_referral_text(user: User) -> str:
    ref_code = user.ref_code or str(user.user_id)
    bot_username = os.getenv("BOT_USERNAME", "BlackBoxGPT_bot")
    link = f"https://t.me/{bot_username}?start=ref_{ref_code}"
    parts = [
        "<b>👥 Реферальная программа</b>",
        "",
        "За каждого друга, который зайдёт по твоей ссылке и начнёт пользоваться ботом,",
        f"ты и он получаете по <b>{REFERRAL_BONUS_DAYS} дню Premium</b>.",
        "",
        "Твоя личная ссылка:",
        link,
        "",
        "Отправь её друзьям или закрепи в своём канале / профиле.",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM integration (DeepSeek)
# ---------------------------------------------------------------------------


async def ask_deepseek(messages: list[Dict[str, str]]) -> str:
    if not DEEPSEEK_API_KEY:
        return "Модель сейчас недоступна (не указан DEEPSEEK_API_KEY)."

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        log.exception("DeepSeek request failed: %s", e)
        return "Что-то пошло не так при обращении к модели. Попробуй ещё раз."

    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        log.error("Unexpected DeepSeek response structure: %s", data)
        return "Не смог разобрать ответ модели. Попробуй ещё раз."


async def make_assistant_reply(user: User, text: str) -> str:
    dossier = get_dossier(user.user_id)
    system_parts = [
        "Ты BlackBox GPT — универсальный русскоязычный ИИ-ассистент в Telegram.",
        "Отвечай ясно, структурированно и по сути.",
    ]

    if user.mode == "deep":
        system_parts.append(
            "Режим: Глубокий разбор. Можно отвечать развёрнуто, с анализом и примерами."
        )
    elif user.mode == "focus":
        system_parts.append(
            "Режим: Фокус / задачи. Помогай формулировать цели, разбивать их на шаги, давай чек-листы."
        )
    elif user.mode == "creative":
        system_parts.append(
            "Режим: Креатив / идеи. Генерируй варианты, примеры, неожиданные решения."
        )
    elif user.mode == "mentor":
        system_parts.append(
            "Режим: Ментор / мотивация. Поддерживай, вдохновляй, но без воды и банальных фраз."
        )
    else:
        system_parts.append("Режим: Универсальный баланс краткости и глубины.")

    if dossier:
        system_parts.append(
            f"Краткий профиль пользователя (используй для персонализации): {dossier}"
        )

    system_prompt = "\n".join(system_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text},
    ]
    answer = await ask_deepseek(messages)
    return answer


async def update_dossier_from_dialog(user: User, user_text: str, assistant_text: str) -> None:
    """Обновляем краткое досье по новым сообщениям."""
    try:
        prev = get_dossier(user.user_id) or ""
        system_prompt = (
            "Ты модуль, который обновляет краткое досье о пользователе для ИИ-ассистента.\n"
            "На основе прошлой версии досье и новых сообщений сформулируй обновлённое досье "
            "в 5–8 предложениях, без повторов и воды.\n"
            "Не пиши от первого лица, только о пользователе."
        )
        user_content = (
            f"Текущее досье (может быть пустым):\n{prev or '(пусто)'}\n\n"
            f"Новое сообщение пользователя:\n{user_text}\n\n"
            f"Ответ ассистента:\n{assistant_text}\n\n"
            "Обнови досье."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        new_dossier = await ask_deepseek(messages)
        save_dossier(user.user_id, new_dossier)
    except Exception:
        log.exception("Failed to update user dossier")


# ---------------------------------------------------------------------------
# Aiogram setup
# ---------------------------------------------------------------------------

router = Router()
dp = Dispatcher()
dp.include_router(router)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    text = message.text or ""
    args: Optional[str] = None
    parts = text.split(maxsplit=1)
    if len(parts) == 2:
        args = parts[1].strip()

    referrer_id: Optional[int] = None
    if args:
        # ожидаем формат ref_<id> или просто число
        if args.startswith("ref_"):
            try:
                referrer_id = int(args.split("_", 1)[1])
            except ValueError:
                referrer_id = None
        else:
            try:
                referrer_id = int(args)
            except ValueError:
                referrer_id = None

    from_user = message.from_user
    user = get_or_create_user(
        user_id=from_user.id,
        username=from_user.username,
        first_name=from_user.first_name,
        last_name=from_user.last_name,
        referrer_id=referrer_id,
    )

    kb = build_main_keyboard(user.mode)

    welcome_lines = [
        "<b>BlackBox GPT — универсальный ИИ-ассистент</b>",
        "",
        "Просто напиши свой запрос — от жизни и работы до креатива и глубоких разборов.",
        "",
        format_modes_hint(user.mode),
        "",
        "На старте доступно 20 бесплатных сообщений. Дальше можно оформить Premium.",
    ]
    await message.answer("\n".join(welcome_lines), reply_markup=kb)


@router.message(F.text == BTN_NEW)
async def on_new_request(message: Message) -> None:
    user = get_user(message.from_user.id)
    mode = user.mode if user else "universal"
    kb = build_main_keyboard(mode)
    await message.answer("Готов. Напиши свой запрос 👇", reply_markup=kb)


@router.message(F.text == BTN_PREMIUM)
async def on_premium(message: Message) -> None:
    user = get_user(message.from_user.id)
    if not user:
        from_user = message.from_user
        user = get_or_create_user(
            user_id=from_user.id,
            username=from_user.username,
            first_name=from_user.first_name,
            last_name=from_user.last_name,
        )
    text = format_premium_text(user)
    kb = build_main_keyboard(user.mode)
    await message.answer(text, reply_markup=kb)


@router.message(F.text == BTN_PROFILE)
async def on_profile(message: Message) -> None:
    user = get_user(message.from_user.id)
    if not user:
        from_user = message.from_user
        user = get_or_create_user(
            user_id=from_user.id,
            username=from_user.username,
            first_name=from_user.first_name,
            last_name=from_user.last_name,
        )
    text = format_profile_text(user)
    kb = build_main_keyboard(user.mode)
    await message.answer(text, reply_markup=kb)


@router.message(F.text == BTN_REFERRAL)
async def on_referral(message: Message) -> None:
    user = get_user(message.from_user.id)
    if not user:
        from_user = message.from_user
        user = get_or_create_user(
            user_id=from_user.id,
            username=from_user.username,
            first_name=from_user.first_name,
            last_name=from_user.last_name,
        )
    text = format_referral_text(user)
    kb = build_main_keyboard(user.mode)
    await message.answer(text, reply_markup=kb)


@router.message(F.text == BTN_CHECK_PAYMENT)
async def on_check_payment(message: Message) -> None:
    user = get_user(message.from_user.id)
    if not user:
        from_user = message.from_user
        user = get_or_create_user(
            user_id=from_user.id,
            username=from_user.username,
            first_name=from_user.first_name,
            last_name=from_user.last_name,
        )

    last_invoice = get_last_invoice(user.user_id)
    kb = build_main_keyboard(user.mode)

    if not last_invoice:
        await message.answer(
            "Пока нет ни одного выставленного счёта.\n"
            "Нажми «⚡ Премиум» и выбери тариф (1m / 3m / 12m).",
            reply_markup=kb,
        )
        return

    invoice_id = last_invoice["invoice_id"]
    await message.answer("Проверяю статус последнего счёта…")

    status_data = await fetch_invoice_status(invoice_id)
    status = (status_data or {}).get("status")

    if status == "paid":
        mark_invoice_paid(invoice_id)
        conn = _get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT plan_code FROM invoices WHERE invoice_id = ?",
                (invoice_id,),
            )
            row = cur.fetchone()
            plan_code = row["plan_code"] if row else "month"
            if plan_code in {"1m", "month"}:
                days = 30
            elif plan_code == "3m":
                days = 90
            else:
                days = 365
            _add_premium_days(conn, user.user_id, days)
            conn.commit()
        finally:
            conn.close()

        await message.answer(
            "Оплата найдена ✅\nПремиум активирован. Спасибо!", reply_markup=kb
        )
    else:
        await message.answer(
            f"Текущий статус счёта: <b>{status or 'unknown'}</b>.\n"
            "Если ты оплатил только что — подожди минуту и нажми кнопку ещё раз.",
            reply_markup=kb,
        )


@router.message()
async def on_message(message: Message) -> None:
    text = (message.text or "").strip()
    if not text:
        return

    from_user = message.from_user
    user = get_or_create_user(
        user_id=from_user.id,
        username=from_user.username,
        first_name=from_user.first_name,
        last_name=from_user.last_name,
    )

    # переключение режима ассистента
    mode_code = detect_mode_from_text(text)
    if mode_code:
        set_user_mode(user.user_id, mode_code)
        user.mode = mode_code
        kb = build_main_keyboard(user.mode)
        await message.answer(
            f"Режим переключён на: <b>{MODES[mode_code]['title']}</b>.",
            reply_markup=kb,
        )
        return

    # выбор тарифа (простые текстовые команды)
    plan_code: str = ""
    low = text.lower()
    if low in {"1m", "1 месяц", "1 месяц — 5 usdt"}:
        plan_code = "1m"
    elif low in {"3m", "3 месяца", "3 месяца — 12 usdt"}:
        plan_code = "3m"
    elif low in {"12m", "12 месяцев", "12 месяцев — 60 usdt"}:
        plan_code = "12m"

    if plan_code:
        kb = build_main_keyboard(user.mode)
        try:
            invoice = await create_invoice(user.user_id, plan_code=plan_code)
        except Exception as e:
            log.exception("Failed to create crypto invoice: %s", e)
            await message.answer(
                "Не удалось создать счёт в Crypto Pay. "
                "Попробуй позже или напиши автору бота.",
                reply_markup=kb,
            )
            return

        invoice_id = int(invoice["invoice_id"])
        save_invoice_record(
            invoice_id=invoice_id,
            user_id=user.user_id,
            plan_code=invoice["plan_code"],
            amount=invoice["amount"],
            asset=invoice["asset"],
            status="active",
        )
        pay_url = invoice["pay_url"]
        await message.answer(
            "Я создал для тебя счёт в Crypto Pay.\n\n"
            f"Сумма: {invoice['amount']} {invoice['asset']}\n"
            f"План: {invoice['plan_code']}\n\n"
            f"Оплатить можно по ссылке:\n{pay_url}\n\n"
            "После оплаты нажми кнопку «🔁 Проверить оплату».",
            reply_markup=kb,
        )
        return

    # обычный запрос к модели
    if not user.is_premium and user.free_used >= FREE_MESSAGES_LIMIT:
        kb = build_main_keyboard(user.mode)
        await message.answer(
            "Ты исчерпал бесплатный лимит сообщений.\n"
            "Чтобы продолжить пользоваться ботом без ограничений — "
            "оформи Premium через кнопку «⚡ Премиум».",
            reply_markup=kb,
        )
        return

    if not user.is_premium:
        increment_free_used(user.user_id)
        user.free_used += 1
        log.info(
            "User %s used free message #%s / %s",
            user.user_id,
            user.free_used,
            FREE_MESSAGES_LIMIT,
        )

    kb = build_main_keyboard(user.mode)
    answer = await make_assistant_reply(user, text)
    await message.answer(answer, reply_markup=kb)

    # обновляем досье в фоне
    asyncio.create_task(update_dossier_from_dialog(user, text, answer))


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    init_db()
    log.info("Starting BlackBox GPT bot polling…")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
