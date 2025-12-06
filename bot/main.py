from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
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
# Тарифы (Crypto / USDT)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    code: str
    title: str
    months: int
    price_usdt: float
    description: str


PLANS: Dict[str, Plan] = {
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
# Режимы работы ассистента
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeConfig:
    key: str
    title: str
    button_text: str
    short_label: str
    system_suffix: str
    description: str


DEFAULT_MODE_KEY = "universal"

MODE_CONFIGS: Dict[str, ModeConfig] = {
    "universal": ModeConfig(
        key="universal",
        title="Универсальный режим",
        button_text="🧠 Универсальный",
        short_label="универсальный умный собеседник",
        description=(
            "Базовый режим для любых тем: от идей и текстов до личных вопросов. "
            "Ответы — структурированные, максимально полезные и спокойные по тону."
        ),
        system_suffix=(
            "Работай как универсальный ИИ-ассистент. "
            "Главная цель — помочь пользователю быстро разобраться в вопросе и принять решение. "
            "Держи баланс между глубиной и краткостью, избегай воды и банальностей."
        ),
    ),
    "medical": ModeConfig(
        key="medical",
        title="Медицинский режим",
        button_text="🩺 Медицина",
        short_label="аккуратный медицинский помощник",
        description=(
            "Осторожные, проверенные ответы по здоровью. "
            "Строгие дисклеймеры и акцент на необходимости очной консультации с врачом."
        ),
        system_suffix=(
            "Сейчас ты работаешь в <медицинском режиме>. "
            "Отвечай крайне аккуратно, только общеобразовательную информацию. "
            "Никогда не ставь диагнозы и не давай прямых назначений. "
            "Всегда проговаривай, что очная консультация врача обязательна.\n\n"
            "Структура ответа:\n"
            "1) Кратко переформулируй запрос.\n"
            "2) Возможные причины и объяснения — без категоричности.\n"
            "3) Что можно сделать аккуратно и безопасно до визита к врачу.\n"
            "4) Красным флагом выдели, при каких симптомах нужно срочно вызвать скорую или обратиться в стационар.\n"
            "5) В конце добавь блок «⚠️ Важно» с напоминанием, что это не замена очной консультации."
        ),
    ),
    "mentor": ModeConfig(
        key="mentor",
        title="Наставник",
        button_text="🔥 Наставник",
        short_label="личный наставник и коуч",
        description=(
            "Фокус на личном росте, дисциплине и мышлении. "
            "В каждом ответе есть конкретные шаги и вопросы для самоанализа."
        ),
        system_suffix=(
            "Сейчас ты работаешь в <режиме наставника и коуча>. "
            "Твоя задача — помогать пользователю расти, усиливать внутренний стержень и ясность.\n\n"
            "Каждый ответ обязательно завершай блоком «👉 Конкретные шаги на сегодня» из 1–3 пунктов. "
            "Не расписывай 20 задач, держи фокус.\n\n"
            "В большинстве ответов (примерно в 70% случаев) задавай один точный уточняющий вопрос "
            "в конце, чтобы углубить рефлексию.\n\n"
            "Учитывай, что пользователь ценит силу, честность и уважение к границам. "
            "Поддерживай, но не убаюкивай — будь прямым, но экологичным."
        ),
    ),
    "business": ModeConfig(
        key="business",
        title="Бизнес-архитектор",
        button_text="💼 Бизнес",
        short_label="бизнес-архитектор",
        description=(
            "Режим для стратегий, запусков и денег. "
            "Максимум конкретики: цифры, гипотезы, тесты, сценарии."
        ),
        system_suffix=(
            "Сейчас ты работаешь в <режиме бизнес-архитектора>. "
            "Фокус — деньги, эффективность и проверяемые гипотезы.\n\n"
            "В каждом ответе используй язык цифр: сроки, бюджеты, метрики, воронки, конверсии там, где это уместно.\n"
            "Обязательно добавляй два блока:\n"
            "• «📊 Что проверить» — список ключевых допущений и рисков.\n"
            "• «🧪 Как протестировать» — простые MVP- и smoke-тесты, которые можно сделать быстро и дёшево.\n"
            "Избегай поверхностных советов вроде «просто делай хороший контент» без конкретики."
        ),
    ),
    "creative": ModeConfig(
        key="creative",
        title="Креативный режим",
        button_text="🎨 Креатив",
        short_label="креативный генератор идей",
        description=(
            "Подходит для идей, образов, текстов и необычных решений. "
            "Более свободный стиль, но без потери структуры."
        ),
        system_suffix=(
            "Сейчас ты работаешь в <креативном режиме>. "
            "Твоя задача — выдавать богатый спектр идей и неожиданных решений, "
            "но не забывать про структуру и применимость.\n\n"
            "Используй примеры, форматы, варианты нейминга и неожиданные ракурсы. "
            "Если уместно — предлагай 2–3 разных подхода: «минималистичный», «смелый», «радикальный»."
        ),
    ),
}

# ---------------------------------------------------------------------------
# База данных
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

        # История сообщений
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                role TEXT NOT NULL,         -- 'user' или 'assistant'
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

        # Миграция: добавляем колонку mode, если её ещё нет
        cur.execute("PRAGMA table_info(users_v2)")
        cols = [row["name"] for row in cur.fetchall()]
        if "mode" not in cols:
            cur.execute("ALTER TABLE users_v2 ADD COLUMN mode TEXT DEFAULT 'universal'")

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
        cur.execute("SELECT * FROM users_v2 WHERE telegram_id = ?", (telegram_id,))
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
                    updated_at_ts,
                    mode
                )
                VALUES (?, ?, ?, ?, 0, NULL, 0, ?, ?, ?)
                """,
                (telegram_id, username, first_name, last_name, now, now, DEFAULT_MODE_KEY),
            )

        conn.commit()

        cur.execute("SELECT * FROM users_v2 WHERE telegram_id = ?", (telegram_id,))
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


def get_user_mode_from_row(user_row: sqlite3.Row) -> str:
    try:
        mode_val = user_row["mode"]
    except (KeyError, IndexError):
        mode_val = None
    if not mode_val or mode_val not in MODE_CONFIGS:
        return DEFAULT_MODE_KEY
    return str(mode_val)


def set_user_mode(telegram_id: int, mode_key: str) -> None:
    if mode_key not in MODE_CONFIGS:
        mode_key = DEFAULT_MODE_KEY
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users_v2 SET mode = ?, updated_at_ts = ? WHERE telegram_id = ?",
            (mode_key, int(time.time()), telegram_id),
        )
        conn.commit()


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


def get_recent_user_messages(telegram_id: int, limit: int = 30) -> List[str]:
    """Получаем последние сообщения пользователя (для анализа стиля)."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT content
            FROM messages
            WHERE telegram_id = ? AND role = 'user'
            ORDER BY created_at_ts DESC
            LIMIT ?
            """,
            (telegram_id, limit),
        )
        rows = cur.fetchall()

    return [row["content"] for row in reversed(rows)]


def get_recent_dialog_history(telegram_id: int, limit: int = 12) -> List[Dict[str, str]]:
    """История диалога user/assistant для передачи в LLM."""
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT role, content
            FROM messages
            WHERE telegram_id = ?
            ORDER BY created_at_ts DESC
            LIMIT ?
            """,
            (telegram_id, limit),
        )
        rows = cur.fetchall()

    result: List[Dict[str, str]] = []
    for row in reversed(rows):
        role = "assistant" if row["role"] == "assistant" else "user"
        result.append({"role": role, "content": row["content"]})
    return result


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

    return f"Адаптируй стиль ответов под пользователя. {tone_hint} {length_hint}"


# ---------------------------------------------------------------------------
# Интенты (двухслойный движок)
# ---------------------------------------------------------------------------


def detect_intent(user_text: str) -> str:
    """
    Очень лёгкий детектор интента.
    Возвращает одно из значений:
    - plan
    - analysis
    - brainstorm
    - emotional
    - other
    """
    text = (user_text or "").lower()

    plan_keywords = [
        "план",
        "по шагам",
        "чек-лист",
        "чеклист",
        "структурируй",
        "roadmap",
        "дорожную карту",
    ]
    if any(k in text for k in plan_keywords):
        return "plan"

    brainstorm_keywords = [
        "идеи",
        "варианты",
        "нейминг",
        "названия",
        "мозговой штурм",
        "brainstorm",
        "как назвать",
    ]
    if any(k in text for k in brainstorm_keywords):
        return "brainstorm"

    emotional_keywords = [
        "мне плохо",
        "тревога",
        "тревожно",
        "страшно",
        "переживаю",
        "мотивация",
        "упал",
        "нет сил",
        "выгорел",
        "выгорание",
    ]
    if any(k in text for k in emotional_keywords):
        return "emotional"

    analysis_keywords = [
        "проанализируй",
        "разбор",
        "анализ",
        "почему",
        "разложи",
        "объясни подробно",
    ]
    if any(k in text for k in analysis_keywords) or len(text) > 600:
        return "analysis"

    return "other"


BASE_SYSTEM_PROMPT = (
    "Ты — BlackBox GPT, универсальный ИИ-ассистент в Telegram.\n"
    "Твои ответы — премиум-класса: структурированные, аккуратные и максимально полезные.\n"
    "Избегай штампов и воды, говори по сути, но по-человечески.\n\n"
    "Всегда отвечай на русском языке, если явно не просят другой язык.\n"
    "Помни, что ты встроен в минималистичный Telegram-бот: интерфейс — только текст и нижний таскбар.\n"
)


def build_system_prompt(mode_key: str, intent: str, style_hint: Optional[str]) -> str:
    mode_cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    if intent == "plan":
        intent_suffix = (
            "Пользователь ожидает прежде всего чёткий план действий.\n"
            "Структурируй ответ как поэтапный план с логичными блоками и подзаголовками.\n"
        )
    elif intent == "analysis":
        intent_suffix = (
            "Пользователь просит глубокий разбор.\n"
            "Сделай аналитический ответ с разбором причин, вариантов и выводом в конце.\n"
        )
    elif intent == "brainstorm":
        intent_suffix = (
            "Пользователь ждёт мозговой штурм.\n"
            "Предложи пул вариантов, сгруппируй их по подходам, рядом с каждым вариантом напиши короткий комментарий.\n"
        )
    elif intent == "emotional":
        intent_suffix = (
            "Пользователь в эмоциональном запросе.\n"
            "Сначала аккуратно отзеркаль состояние и прояви поддержку, затем предложи простые, реалистичные шаги.\n"
        )
    else:
        intent_suffix = (
            "Формат ответа выбирай сам, исходя из запроса, но помни про структуру и ясность мысли.\n"
        )

    parts: List[str] = [
        BASE_SYSTEM_PROMPT,
        mode_cfg.system_suffix,
        intent_suffix,
    ]
    if style_hint:
        parts.append(style_hint)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# LLM (DeepSeek / Groq)
# ---------------------------------------------------------------------------


async def _call_deepseek(
    user_text: str,
    mode_key: str,
    intent: str,
    style_hint: Optional[str],
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")

    sys_prompt = build_system_prompt(mode_key, intent, style_hint)

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    messages: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    if history:
        messages.extend(history[-10:])
    else:
        messages.append({"role": "user", "content": user_text})

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
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


async def _call_groq(
    user_text: str,
    mode_key: str,
    intent: str,
    style_hint: Optional[str],
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured")

    sys_prompt = build_system_prompt(mode_key, intent, style_hint)

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    messages: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    if history:
        messages.extend(history[-10:])
    else:
        messages.append({"role": "user", "content": user_text})

    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
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


async def generate_ai_reply(
    user_text: str,
    mode_key: str,
    style_hint: Optional[str] = None,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """
    Универсальный генератор ответа: DeepSeek → Groq → fallback.
    Учитывает style_hint, активный режим и лёгкий интент.
    """
    intent = detect_intent(user_text)
    last_error: Optional[Exception] = None

    if DEEPSEEK_API_KEY:
        try:
            return await _call_deepseek(user_text, mode_key, intent, style_hint, history)
        except Exception as e:  # noqa: BLE001
            last_error = e
            logger.exception("DeepSeek API error: %r", e)

    if GROQ_API_KEY:
        try:
            return await _call_groq(user_text, mode_key, intent, style_hint, history)
        except Exception as e:  # noqa: BLE001
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


async def crypto_create_invoice(plan: Plan, telegram_id: int) -> Dict:
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
        "asset": "USDT",
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

    if is_user_admin(username):
        return True, user

    if user_is_premium(user):
        return True, user

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
# UI: таскбар
# ---------------------------------------------------------------------------

BTN_MODES = "🧠 Режимы"
BTN_PROFILE = "👤 Профиль"
BTN_SUBSCRIPTION = "💎 Подписка"
BTN_REFERRALS = "👥 Рефералы"

BTN_MODE_UNIVERSAL = MODE_CONFIGS["universal"].button_text
BTN_MODE_MEDICAL = MODE_CONFIGS["medical"].button_text
BTN_MODE_MENTOR = MODE_CONFIGS["mentor"].button_text
BTN_MODE_BUSINESS = MODE_CONFIGS["business"].button_text
BTN_MODE_CREATIVE = MODE_CONFIGS["creative"].button_text

BTN_BACK = "⬅️ Назад"

BTN_PLAN_1M = "💎 1 месяц"
BTN_PLAN_3M = "💎 3 месяца"
BTN_PLAN_12M = "💎 12 месяцев"

PLAN_BUTTON_TO_CODE: Dict[str, str] = {
    BTN_PLAN_1M: "1m",
    BTN_PLAN_3M: "3m",
    BTN_PLAN_12M: "12m",
}


def main_taskbar() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODES), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_SUBSCRIPTION), KeyboardButton(text=BTN_REFERRALS)],
        ],
        resize_keyboard=True,
    )


def modes_taskbar() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_MODE_UNIVERSAL), KeyboardButton(text=BTN_MODE_MEDICAL)],
            [KeyboardButton(text=BTN_MODE_MENTOR), KeyboardButton(text=BTN_MODE_BUSINESS)],
            [KeyboardButton(text=BTN_MODE_CREATIVE), KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def subscription_taskbar() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_PLAN_1M), KeyboardButton(text=BTN_PLAN_3M)],
            [KeyboardButton(text=BTN_PLAN_12M), KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


MENU_TEXTS = {
    BTN_MODES,
    BTN_PROFILE,
    BTN_SUBSCRIPTION,
    BTN_REFERRALS,
    BTN_MODE_UNIVERSAL,
    BTN_MODE_MEDICAL,
    BTN_MODE_MENTOR,
    BTN_MODE_BUSINESS,
    BTN_MODE_CREATIVE,
    BTN_BACK,
    BTN_PLAN_1M,
    BTN_PLAN_3M,
    BTN_PLAN_12M,
}

# ---------------------------------------------------------------------------
# Живое печатание
# ---------------------------------------------------------------------------

STREAM_CHUNK_SIZE = 80
STREAM_MAX_STEPS = 40
STREAM_DELAY_SECONDS = 0.12


def _chunk_text_for_streaming(text: str) -> List[str]:
    text = text.strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    raw_chunks: List[str] = []

    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            continue

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

        raw_chunks.append("\n")

    if raw_chunks and raw_chunks[-1] == "\n":
        raw_chunks.pop()

    if len(raw_chunks) <= STREAM_MAX_STEPS:
        return raw_chunks

    step = math.ceil(len(raw_chunks) / STREAM_MAX_STEPS)
    chunks: List[str] = []
    for i in range(0, len(raw_chunks), step):
        chunks.append(" ".join(raw_chunks[i : i + step]).replace(" \n ", "\n"))
    return chunks


async def stream_reply_text(message: Message, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return

    chunks = _chunk_text_for_streaming(text)

    if len(chunks) <= 1:
        await message.answer(text)
        return

    bot = message.bot

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    current = chunks[0]
    sent = await message.answer(current)

    for chunk in chunks[1:]:
        await asyncio.sleep(STREAM_DELAY_SECONDS)
        current = f"{current} {chunk}".strip()

        try:
            await bot.edit_message_text(
                current,
                chat_id=sent.chat.id,
                message_id=sent.message_id,
                parse_mode=None,
            )
            await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        except Exception as e:  # noqa: BLE001
            logger.exception("Streaming edit error: %r", e)
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
    mode_key = get_user_mode_from_row(user_row)
    mode_cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    name = message.from_user.first_name or "друг"

    if is_premium:
        premium_line = (
            "<b>Статус:</b> Premium-аккаунт активен — лимитов на диалог нет.\n\n"
        )
    else:
        used = int(user_row["free_used"] or 0)
        left = max(FREE_MESSAGES_LIMIT - used, 0)
        premium_line = (
            f"<b>Статус:</b> базовый доступ. Доступно <b>{left}</b> бесплатных сообщений, "
            f"после — можно оформить подписку через USDT.\n\n"
        )

    text = (
        f"<b>Привет, {name}!</b>\n\n"
        "<b>BlackBox GPT</b> — универсальный ИИ-ассистент премиум-класса.\n"
        "Минимализм во всём: только диалог и нижний таскбар.\n\n"
        "<b>Как со мной работать:</b>\n"
        "• просто задай первый вопрос в чате — от медицины и бизнеса до личного развития;\n"
        "• выбери режим внизу, если нужен особый фокус (наставник, медицина, бизнес и т.д.).\n\n"
        f"{premium_line}"
        f"<b>Текущий режим:</b> {mode_cfg.title} — {mode_cfg.short_label}.\n\n"
        "Пиши, чем тебе помочь прямо сейчас."
    )

    await message.answer(text, reply_markup=main_taskbar())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Что умеет BlackBox GPT</b>\n\n"
        "• разбирает сложные ситуации и помогает принять решение;\n"
        "• составляет планы, чек-листы, стратегии и roadmaps;\n"
        "• помогает с текстами, идеями, неймингом и креативом;\n"
        "• подсказывает по коду и технологиям;\n"
        "• аккуратно даёт справочную медицинскую информацию (но не ставит диагнозы).\n\n"
        "<b>Команды:</b>\n"
        "/start — перезапустить приветствие и главное меню;\n"
        "/subscription — открыть раздел с подпиской.\n\n"
        "Дальше можно просто общаться текстом — как с живым умным собеседником."
    )
    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Режимы --------------------------


@router.message(F.text == BTN_MODES)
async def show_modes_menu(message: Message) -> None:
    text = (
        "<b>Режимы BlackBox GPT</b>\n\n"
        "Выбери, в каком фокусе сейчас нужен ассистент:\n"
        f"• {MODE_CONFIGS['universal'].short_label};\n"
        f"• {MODE_CONFIGS['medical'].short_label};\n"
        f"• {MODE_CONFIGS['mentor'].short_label};\n"
        f"• {MODE_CONFIGS['business'].short_label};\n"
        f"• {MODE_CONFIGS['creative'].short_label}.\n\n"
        "Нажми нужный режим на таскбаре ниже — я сразу подстрою стиль ответов."
    )
    await message.answer(text, reply_markup=modes_taskbar())


async def _set_mode_and_confirm(message: Message, mode_key: str) -> None:
    user_id = message.from_user.id
    set_user_mode(user_id, mode_key)
    cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    text = (
        f"<b>Режим установлен:</b> {cfg.title}.\n\n"
        f"{cfg.description}\n\n"
        "Просто напиши следующий запрос — я отвечу в этом режиме."
    )
    await message.answer(text, reply_markup=main_taskbar())


@router.message(F.text == BTN_MODE_UNIVERSAL)
async def set_mode_universal(message: Message) -> None:
    await _set_mode_and_confirm(message, "universal")


@router.message(F.text == BTN_MODE_MEDICAL)
async def set_mode_medical(message: Message) -> None:
    await _set_mode_and_confirm(message, "medical")


@router.message(F.text == BTN_MODE_MENTOR)
async def set_mode_mentor(message: Message) -> None:
    await _set_mode_and_confirm(message, "mentor")


@router.message(F.text == BTN_MODE_BUSINESS)
async def set_mode_business(message: Message) -> None:
    await _set_mode_and_confirm(message, "business")


@router.message(F.text == BTN_MODE_CREATIVE)
async def set_mode_creative(message: Message) -> None:
    await _set_mode_and_confirm(message, "creative")


# -------------------------- Профиль --------------------------


@router.message(F.text == BTN_PROFILE)
async def show_profile(message: Message) -> None:
    user_row = await _ensure_user(message)
    is_premium = user_is_premium(user_row)
    mode_key = get_user_mode_from_row(user_row)
    mode_cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    used = int(user_row["free_used"] or 0)
    left = max(FREE_MESSAGES_LIMIT - used, 0)

    if is_premium:
        status_line = "Premium-доступ активен. Лимитов по сообщениям нет."
    else:
        status_line = (
            f"Базовый доступ. Использовано <b>{used}</b> бесплатных сообщений "
            f"из <b>{FREE_MESSAGES_LIMIT}</b>. Осталось ≈ <b>{left}</b>."
        )

    text = (
        "<b>Профиль</b>\n\n"
        f"<b>Аккаунт:</b> @{message.from_user.username or 'без username'}\n"
        f"<b>Статус:</b> {status_line}\n"
        f"<b>Текущий режим:</b> {mode_cfg.title} — {mode_cfg.short_label}.\n\n"
        "<b>Диалоговая память:</b> активна.\n"
        "Я учитываю последние сообщения, чтобы лучше подстраиваться под твой стиль общения.\n\n"
        "Если захочешь, в будущем сюда можно добавить цели, привычки и персональные настройки."
    )
    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Рефералы --------------------------


@router.message(F.text == BTN_REFERRALS)
async def show_referrals(message: Message) -> None:
    text = (
        "<b>Реферальная система</b>\n\n"
        "Скоро здесь появится персональная ссылка, по которой можно приглашать людей в бот "
        "и получать бонусы: дополнительные сообщения, доступ к новым режимам и другие плюшки.\n\n"
        "Механика будет продумана так, чтобы это выглядело честно и выгодно и для тебя, и для друзей.\n\n"
        "А пока можешь просто пользоваться ботом и думать, кому бы ты его первым делом показал."
    )
    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Подписка --------------------------


async def _subscription_overview_text(user_row: sqlite3.Row) -> str:
    is_premium = user_is_premium(user_row)

    if is_premium:
        header = "<b>Подписка BlackBox GPT</b>\n\nПремиум уже активен — продлить можно в любой момент.\n\n"
    else:
        used = int(user_row["free_used"] or 0)
        left = max(FREE_MESSAGES_LIMIT - used, 0)
        header = (
            "<b>Подписка BlackBox GPT</b>\n\n"
            f"Сейчас у тебя базовый доступ и ≈ <b>{left}</b> бесплатных сообщений.\n"
            "Подписка снимает ограничения и даёт приоритетную скорость обработки запросов.\n\n"
        )

    lines = [header, "<b>Тарифы (оплата в USDT через Crypto Bot):</b>"]
    for code in ("1m", "3m", "12m"):
        plan = PLANS[code]
        lines.append(f"• {plan.title} — {plan.price_usdt:.2f} USDT")

    lines.append(
        "\nВыбери тариф на таскбаре ниже, я создам персональную ссылку на оплату в Crypto Bot."
    )
    return "\n".join(lines)


@router.message(Command("subscription"))
@router.message(F.text == BTN_SUBSCRIPTION)
async def show_subscription(message: Message) -> None:
    user_row = await _ensure_user(message)
    text = await _subscription_overview_text(user_row)
    await message.answer(text, reply_markup=subscription_taskbar())


@router.message(F.text.in_(list(PLAN_BUTTON_TO_CODE.keys())))
async def handle_subscription_plan(message: Message) -> None:
    plan_code = PLAN_BUTTON_TO_CODE[message.text]
    plan = PLANS.get(plan_code)
    if not plan:
        await message.answer(
            "Не удалось определить тариф. Попробуй выбрать ещё раз.",
            reply_markup=subscription_taskbar(),
        )
        return

    if not CRYPTO_ENABLED:
        await message.answer(
            "Платёжный модуль ещё не настроен. Свяжись с админом, если хочешь протестировать оплату.",
            reply_markup=main_taskbar(),
        )
        return

    try:
        invoice = await crypto_create_invoice(plan, message.from_user.id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Error while creating invoice: %r", e)
        await message.answer(
            "Произошла ошибка при создании счёта.\nПопробуй ещё раз чуть позже.",
            reply_markup=subscription_taskbar(),
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
        await message.answer(
            "Не удалось получить ссылку на оплату.\nПопробуй позже или напиши администратору.",
            reply_markup=subscription_taskbar(),
        )
        return

    text = (
        "<b>Оформление подписки BlackBox GPT</b>\n\n"
        f"<b>План:</b> {plan.title}\n"
        f"<b>Сумма:</b> {plan.price_usdt:.2f} USDT\n\n"
        "Ссылка на оплату через Crypto Bot:\n"
        f"{pay_url}\n\n"
        "После успешной оплаты премиум-доступ будет активирован автоматически (когда подключим вебхук) "
        "или вручную администратором."
    )
    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Назад --------------------------


@router.message(F.text == BTN_BACK)
async def handle_back(message: Message) -> None:
    text = (
        "Возвращаю тебя на главный экран.\n"
        "Снизу снова универсальный таскбар: режимы, профиль, подписка и рефералы."
    )
    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Админ --------------------------


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
            "/grant_premium <telegram_id|@username> <месяцев>\n\n"
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

    if months <= 0:
        await message.reply("Количество месяцев должно быть положительным.")
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
        f"Премиум на {months} мес. выдан пользователю <code>{telegram_id}</code>.",
        parse_mode=ParseMode.HTML,
    )


# -------------------------- Диалог --------------------------


@router.message(F.chat.type == ChatType.PRIVATE)
async def handle_private_chat(message: Message) -> None:
    """
    Общий хэндлер для диалога с ИИ.
    """
    if not message.text:
        return

    if message.text in MENU_TEXTS:
        return

    if message.text.startswith("/"):
        return

    allowed, user_row = await _check_access(message)
    if not allowed:
        used = get_free_used(message.from_user.id)
        text = (
            "⚠️ Лимит бесплатных сообщений исчерпан.\n\n"
            f"Ты уже использовал {used} / {FREE_MESSAGES_LIMIT}.\n\n"
            "Чтобы продолжить общение без ограничений, нажми «💎 Подписка» внизу и оформи премиум-доступ."
        )
        await message.answer(text, reply_markup=main_taskbar())
        return

    user_id = message.from_user.id

    save_message(user_id, "user", message.text)

    style_hint = build_style_hint(user_id)

    history = get_recent_dialog_history(user_id, limit=12)

    text_len = len(message.text.strip())
    if text_len < 120:
        style_hint = (
            (style_hint + "\n\n") if style_hint else ""
        ) + (
            "Сейчас запрос короткий. Сделай ответ компактным (2–4 абзаца или список до 7 пунктов). "
            "В конце одной строкой пригласи пользователя написать «Раскрой подробнее», "
            "если ему захочется углубиться."
        )
        use_stream = False
    else:
        style_hint = (
            (style_hint + "\n\n") if style_hint else ""
        ) + (
            "Запрос достаточно объёмный. Дай глубокий, хорошо структурированный разбор с подзаголовками. "
            "В конце можно добавить строку, что при желании ты продолжишь развернуть тему."
        )
        use_stream = True

    mode_key = get_user_mode_from_row(user_row)

    reply = await generate_ai_reply(
        message.text,
        mode_key=mode_key,
        style_hint=style_hint,
        history=history,
    )

    save_message(user_id, "assistant", reply)

    if use_stream:
        await stream_reply_text(message, reply)
    else:
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
