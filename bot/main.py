from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Tuple

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

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")
CRYPTO_DEFAULT_ASSET = os.getenv("CRYPTO_DEFAULT_ASSET", "USDT")

DB_PATH = os.getenv("DB_PATH", "aimedbot.db")

FREE_MESSAGES_LIMIT = int(os.getenv("FREE_MESSAGES_LIMIT", "20"))

ADMIN_USERNAMES = {
    u.strip().lower()
    for u in os.getenv("ADMIN_USERNAMES", "").replace(",", " ").split()
    if u.strip()
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment")

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
# Режимы ассистента
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModeConfig:
    key: str
    title: str
    button_text: str
    short_label: str
    description: str
    system_suffix: str


DEFAULT_MODE_KEY = "universal"

MODE_CONFIGS: Dict[str, ModeConfig] = {
    "universal": ModeConfig(
        key="universal",
        title="Универсальный режим",
        button_text="🧠 Универсальный",
        short_label="универсальный умный собеседник",
        description=(
            "Режим по умолчанию: подходит и для размышлений, и для задач, и для текстов. "
            "Баланс между глубиной и скоростью ответа."
        ),
        system_suffix=(
            "Ты работаешь в универсальном режиме. "
            "Главная цель — быстро и по-человечески помочь пользователю разобраться в вопросе. "
            "Избегай штампов и размытых формулировок, отвечай структурно."
        ),
    ),
    "medical": ModeConfig(
        key="medical",
        title="Медицинский режим",
        button_text="🩺 Медицина",
        short_label="аккуратный медицинский помощник",
        description=(
            "Осторожные, проверенные ответы по здоровью. "
            "Всегда с дисклеймером, что это не замена очной консультации."
        ),
        system_suffix=(
            "Сейчас ты работаешь в медицинском режиме. "
            "Давай только общеобразовательную информацию, опираясь на доказательный подход. "
            "Никогда не ставь диагнозы и не давай прямых назначений лекарств.\n\n"
            "Структура ответа:\n"
            "1) Кратко переформулируй запрос.\n"
            "2) Возможные объяснения и факторы — без категоричности.\n"
            "3) Что можно сделать аккуратно и безопасно до визита к врачу.\n"
            "4) Когда нужно немедленно обратиться за очной помощью.\n"
            "5) В конце добавь блок «⚠️ Важно», что это не замена консультации врача."
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
            "Сейчас ты работаешь в режиме наставника и коуча. "
            "Твоя задача — усиливать стержень пользователя и помогать ему двигаться вперёд.\n\n"
            "Каждый ответ обязательно завершай блоком «👉 Конкретные шаги на сегодня» из 1–3 пунктов. "
            "В большинстве ответов задавай в конце один точный вопрос для саморефлексии."
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
            "Сейчас ты работаешь в режиме бизнес-архитектора. "
            "Фокус — деньги, эффективность и проверяемые гипотезы.\n\n"
            "Используй язык цифр и метрик там, где это уместно. "
            "В ответах добавляй два блока:\n"
            "• «📊 Что проверить» — ключевые допущения и риски.\n"
            "• «🧪 Как протестировать» — простые шаги для MVP и smoke-тестов."
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
            "Сейчас ты работаешь в креативном режиме. "
            "Твоя задача — выдавать богатый спектр идей и неожиданных решений, "
            "не забывая про практическую применимость.\n\n"
            "Предлагай несколько подходов, давай варианты формулировок, названий, визуальных концептов."
        ),
    ),
}

BASE_SYSTEM_PROMPT = (
    "Ты — BlackBox GPT, универсальный ИИ-ассистент в Telegram.\n"
    "Интерфейс — минималистичный чат: никакого визуального шума, только текст высокого качества.\n"
    "Твои ответы должны восприниматься как работа премиум-уровня: ясная структура, аккуратный язык, уважительный тон.\n"
    "Всегда отвечай на русском языке, если явно не попросили другой язык.\n"
)

# ---------------------------------------------------------------------------
# Style Engine 2.0 — профиль стиля пользователя
# ---------------------------------------------------------------------------


@dataclass
class StyleProfile:
    """
    Профиль стиля общения конкретного пользователя.

    Все параметры в диапазоне 0..1, где 0 — один полюс, 1 — другой.
    """

    # 'ty' / 'vy'
    address: str = "ty"

    # 0 — разговорный, 0.5 — нейтральный, 1 — деловой
    formality: float = 0.5

    # плотность структуры: 0 — «полотно», 1 — много списков и заголовков
    structure_density: float = 0.5

    # глубина объяснений: 0 — очень кратко, 1 — максимально развёрнуто
    explanation_depth: float = 0.5

    # уровень «огня»: 0 — очень мягко, 1 — максимально прямолинейно и жёстко
    fire_level: float = 0.3

    updated_at_ts: float = field(default_factory=lambda: time.time())


# ---------------------------------------------------------------------------
# Тарифы (подписка)
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
        description="Стартовый доступ к BlackBox GPT на 1 месяц.",
    ),
    "3m": Plan(
        code="3m",
        title="3 месяца доступа",
        months=3,
        price_usdt=26.99,
        description="Удобный пакет на 3 месяца со скидкой.",
    ),
    "12m": Plan(
        code="12m",
        title="12 месяцев доступа",
        months=12,
        price_usdt=82.99,
        description="Годовой доступ с максимальной выгодой.",
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
    conn = _get_conn()
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
            mode TEXT NOT NULL DEFAULT 'universal',
            free_used INTEGER NOT NULL DEFAULT 0,
            is_premium INTEGER NOT NULL DEFAULT 0,
            premium_until_ts INTEGER,
            created_at_ts INTEGER NOT NULL,
            updated_at_ts INTEGER NOT NULL
        )
        """
    )

    # Гарантируем, что в таблице есть колонка для StyleProfile
    try:
        cur.execute("PRAGMA table_info(users_v2)")
        cols = [row["name"] for row in cur.fetchall()]
        if "style_profile_json" not in cols:
            cur.execute("ALTER TABLE users_v2 ADD COLUMN style_profile_json TEXT")
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to ensure style_profile_json column: %r", e)

    # История сообщений
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at_ts INTEGER NOT NULL
        )
        """
    )

    # Реферальные связи
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_id INTEGER NOT NULL,
            invited_id INTEGER NOT NULL,
            created_at_ts INTEGER NOT NULL,
            UNIQUE(inviter_id, invited_id)
        )
        """
    )

    # Счета Crypto Pay
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER UNIQUE NOT NULL,
            telegram_id INTEGER NOT NULL,
            plan_code TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at_ts INTEGER NOT NULL,
            updated_at_ts INTEGER NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def get_or_create_user(
    telegram_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
) -> sqlite3.Row:
    now_ts = int(time.time())
    conn = _get_conn()
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
            (username, first_name, last_name, now_ts, telegram_id),
        )
    else:
        cur.execute(
            """
            INSERT INTO users_v2 (
                telegram_id,
                username,
                first_name,
                last_name,
                mode,
                free_used,
                is_premium,
                premium_until_ts,
                created_at_ts,
                updated_at_ts
            )
            VALUES (?, ?, ?, ?, ?, 0, 0, NULL, ?, ?)
            """,
            (telegram_id, username, first_name, last_name, DEFAULT_MODE_KEY, now_ts, now_ts),
        )

    conn.commit()
    cur.execute("SELECT * FROM users_v2 WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM users_v2 WHERE lower(username) = ?",
        (username.lower(),),
    )
    row = cur.fetchone()
    conn.close()
    return row


def user_is_premium(user_row: sqlite3.Row) -> bool:
    until = user_row["premium_until_ts"]
    if not until:
        return False
    try:
        return int(until) > int(time.time())
    except (ValueError, TypeError):
        return False


def grant_premium(telegram_id: int, months: int) -> None:
    extend_seconds = int(months * 30.4375 * 24 * 3600)
    now_ts = int(time.time())

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT premium_until_ts FROM users_v2 WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = cur.fetchone()
    current_until = int(row["premium_until_ts"]) if row and row["premium_until_ts"] else 0

    base = current_until if current_until > now_ts else now_ts
    new_until = base + extend_seconds

    cur.execute(
        """
        UPDATE users_v2
        SET is_premium = 1, premium_until_ts = ?, updated_at_ts = ?
        WHERE telegram_id = ?
        """,
        (new_until, now_ts, telegram_id),
    )
    conn.commit()
    conn.close()


def get_free_used(telegram_id: int) -> int:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT free_used FROM users_v2 WHERE telegram_id = ?",
        (telegram_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row or row["free_used"] is None:
        return 0
    return int(row["free_used"])


def increment_free_used(telegram_id: int, delta: int = 1) -> int:
    conn = _get_conn()
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
    conn.close()
    if not row or row["free_used"] is None:
        return delta
    return int(row["free_used"])


def is_user_admin(username: Optional[str]) -> bool:
    return bool(username and username.lower() in ADMIN_USERNAMES)


def get_user_mode_from_row(user_row: sqlite3.Row) -> str:
    keys = {k for k in user_row.keys()}
    mode_val = user_row["mode"] if "mode" in keys else None
    if not mode_val or mode_val not in MODE_CONFIGS:
        return DEFAULT_MODE_KEY
    return str(mode_val)


def set_user_mode(telegram_id: int, mode_key: str) -> None:
    if mode_key not in MODE_CONFIGS:
        mode_key = DEFAULT_MODE_KEY
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users_v2 SET mode = ?, updated_at_ts = ? WHERE telegram_id = ?",
        (mode_key, int(time.time()), telegram_id),
    )
    conn.commit()
    conn.close()


def save_message(telegram_id: int, role: str, content: str) -> None:
    content = (content or "").strip()
    if not content:
        return

    ts = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO messages (telegram_id, role, content, created_at_ts)
        VALUES (?, ?, ?, ?)
        """,
        (telegram_id, role, content, ts),
    )
    conn.commit()
    conn.close()


def get_recent_user_messages(telegram_id: int, limit: int = 30) -> List[str]:
    conn = _get_conn()
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
    conn.close()
    return [row["content"] for row in reversed(rows)]


def get_recent_dialog_history(telegram_id: int, limit: int = 12) -> List[Dict[str, str]]:
    conn = _get_conn()
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
    conn.close()

    history: List[Dict[str, str]] = []
    for row in reversed(rows):
        role = "assistant" if row["role"] == "assistant" else "user"
        history.append({"role": role, "content": row["content"]})
    return history


def get_user_stats(telegram_id: int) -> Dict[str, Optional[int]]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT created_at_ts, premium_until_ts, free_used
        FROM users_v2
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    )
    row = cur.fetchone()

    if row:
        created = int(row["created_at_ts"]) if row["created_at_ts"] else None
        premium_until = int(row["premium_until_ts"]) if row["premium_until_ts"] else None
        free_used = int(row["free_used"] or 0)
    else:
        created = premium_until = free_used = None

    cur.execute(
        "SELECT COUNT(*) AS cnt FROM messages WHERE telegram_id = ?",
        (telegram_id,),
    )
    msg_row = cur.fetchone()
    message_count = int(msg_row["cnt"] or 0) if msg_row else 0

    conn.close()
    return {
        "created_at_ts": created,
        "premium_until_ts": premium_until,
        "free_used": free_used,
        "message_count": message_count,
    }


# ---------------------------------------------------------------------------
# Стиль общения: загрузка/сохранение StyleProfile
# ---------------------------------------------------------------------------


def _style_profile_from_dict(data: Dict[str, Any]) -> StyleProfile:
    return StyleProfile(
        address=str(data.get("address", "ty")) if data.get("address") in {"ty", "vy"} else "ty",
        formality=float(data.get("formality", 0.5)),
        structure_density=float(data.get("structure_density", 0.5)),
        explanation_depth=float(data.get("explanation_depth", 0.5)),
        fire_level=float(data.get("fire_level", 0.3)),
        updated_at_ts=float(data.get("updated_at_ts", time.time())),
    )


def _load_style_profile(telegram_id: int) -> Optional[StyleProfile]:
    conn = _get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT style_profile_json FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
    except sqlite3.OperationalError:
        conn.close()
        return None
    conn.close()

    if not row:
        return None

    raw = row["style_profile_json"]
    if not raw:
        return None

    try:
        data = json.loads(raw)
        return _style_profile_from_dict(data)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to parse style_profile_json for %s: %r", telegram_id, e)
        return None


def _save_style_profile(telegram_id: int, profile: StyleProfile) -> None:
    conn = _get_conn()
    cur = conn.cursor()
    data_json = json.dumps(asdict(profile), ensure_ascii=False)
    cur.execute(
        """
        UPDATE users_v2
        SET style_profile_json = ?, updated_at_ts = ?
        WHERE telegram_id = ?
        """,
        (data_json, int(time.time()), telegram_id),
    )
    conn.commit()
    conn.close()


def _instant_style_from_messages(messages: List[str]) -> StyleProfile:
    """
    Быстрая оценка стиля по последним сообщениям.
    Это «моментальный снимок», который потом смешиваем с уже накопленным профилем.
    """
    if not messages:
        return StyleProfile()

    joined = " ".join(messages)
    lower = joined.lower()

    # Обращение / формальность
    formal_markers = ["здравствуйте", "добрый день", "добрый вечер", "уважаем", "будьте добры"]
    slang_markers = ["чувак", "бро", "фигня", "жесть", "капец"]

    uses_vy = any(m in lower for m in formal_markers) or " вы " in lower
    uses_ty_slang = any(m in lower for m in slang_markers) or " ты " in lower

    if uses_vy and not uses_ty_slang:
        address = "vy"
        formality = 0.85
    elif uses_ty_slang and not uses_vy:
        address = "ty"
        formality = 0.25
    else:
        address = "ty"
        formality = 0.5

    # Структура
    has_lists = any(
        marker in joined
        for marker in ["\n- ", "\n•", "\n1.", "\n1)", "1) ", "1. "]
    )
    structure_density = 0.75 if has_lists else 0.35

    # Глубина объяснений
    lengths = [len(m) for m in messages if m.strip()]
    avg_len = sum(lengths) / len(lengths) if lengths else 0
    if avg_len < 80:
        explanation_depth = 0.25
    elif avg_len < 220:
        explanation_depth = 0.5
    else:
        explanation_depth = 0.8

    # Уровень «огня»
    fire_level = 0.3
    strong_words = [
        "нах",
        "хрен",
        "черт",
        "чёрт",
        "дерьмо",
        "сраная",
        "сраный",
        "жестко",
        "жёстко",
        "рубить правду",
        "по-жёсткому",
    ]
    soft_words = ["помягче", "бережно", "аккуратнее"]

    if any(w in lower for w in strong_words):
        fire_level = 0.7
    if any(w in lower for w in soft_words):
        fire_level = 0.2

    return StyleProfile(
        address=address,
        formality=formality,
        structure_density=structure_density,
        explanation_depth=explanation_depth,
        fire_level=fire_level,
    )


def build_style_profile_from_history(
    telegram_id: int,
) -> StyleProfile:
    """
    Строим/обновляем StyleProfile по последним сообщениям пользователя,
    используя скользящее среднее относительно предыдущего профиля.
    """
    messages = get_recent_user_messages(telegram_id, limit=30)
    snapshot = _instant_style_from_messages(messages)
    prev = _load_style_profile(telegram_id)

    if not prev:
        profile = snapshot
    else:
        alpha = 0.25  # вес нового поведения
        profile = StyleProfile(
            address=snapshot.address if snapshot.address != prev.address else prev.address,
            formality=prev.formality * (1 - alpha) + snapshot.formality * alpha,
            structure_density=prev.structure_density * (1 - alpha)
            + snapshot.structure_density * alpha,
            explanation_depth=prev.explanation_depth * (1 - alpha)
            + snapshot.explanation_depth * alpha,
            fire_level=prev.fire_level * (1 - alpha) + snapshot.fire_level * alpha,
            updated_at_ts=time.time(),
        )

    _save_style_profile(telegram_id, profile)
    return profile


def style_profile_to_hint(profile: StyleProfile) -> str:
    """
    Превращаем StyleProfile в текстовую подсказку для LLM.
    """
    parts: List[str] = ["Адаптируй стиль под пользователя."]

    # Обращение
    if profile.address == "vy":
        parts.append("Обращайся к пользователю на «Вы», без фамильярности.")
    else:
        parts.append("Обращайся к пользователю на «ты», живо, но без панибратства.")

    # Формальность
    if profile.formality > 0.7:
        parts.append("Стиль ближе к деловому: аккуратные формулировки, минимум сленга.")
    elif profile.formality < 0.3:
        parts.append("Стиль ближе к разговорному: допускается живой язык, но без грубостей.")
    else:
        parts.append("Стиль нейтральный: можно чуть живого языка, но без канцелярита и без жаргона.")

    # Структура
    if profile.structure_density > 0.65:
        parts.append(
            "Структуруй ответы: используй подзаголовки и списки там, где это помогает быстро считывать смысл."
        )
    elif profile.structure_density < 0.35:
        parts.append(
            "Можно отвечать цельным текстом, без избытка списков, главное — логика и плавность."
        )
    else:
        parts.append(
            "Комбинируй абзацы и короткие списки так, чтобы текст был и живым, и читаемым."
        )

    # Глубина объяснений
    if profile.explanation_depth < 0.35:
        parts.append(
            "Даёшь суть кратко: 2–4 абзаца или список до 7 пунктов, без повторов и воды."
        )
    elif profile.explanation_depth > 0.7:
        parts.append(
            "Пользователь нормально воспринимает развёрнутые ответы — можно углубляться, но держи структуру."
        )
    else:
        parts.append(
            "Держи баланс: достаточно деталей, чтобы было понятно, но без перегруза техническими тонкостями."
        )

    # Уровень «огня»
    if profile.fire_level > 0.7:
        parts.append(
            "Можно быть довольно прямым и жёстким, но не переходи на личности и не используй агрессию."
        )
    elif profile.fire_level < 0.25:
        parts.append(
            "Формулируй мягко и поддерживающе, без морализаторства и давления, особенно в личных темах."
        )
    else:
        parts.append(
            "Позволяй себе честную прямоту, но обрамляй её в уважительный и конструктивный тон."
        )

    return " ".join(parts)


def style_profile_to_summary(profile: StyleProfile) -> str:
    """
    Краткое описание стиля, которое показываем в профиле.
    """
    addr = "общение на «Вы»" if profile.address == "vy" else "общение на «ты»"

    if profile.formality > 0.7:
        frm = "деловой, аккуратный тон"
    elif profile.formality < 0.3:
        frm = "разговорный, свободный тон"
    else:
        frm = "нейтральный стиль общения"

    if profile.structure_density > 0.65:
        struct = "любит списки и чёткую структуру"
    elif profile.structure_density < 0.35:
        struct = "чаще пишет «полотном» без жёстких списков"
    else:
        struct = "комбинирует абзацы и списки по ситуации"

    if profile.explanation_depth < 0.35:
        depth = "предпочитает, когда всё максимально кратко"
    elif profile.explanation_depth > 0.7:
        depth = "нормально воспринимает развёрнутые объяснения"
    else:
        depth = "оптимален средний уровень деталей"

    if profile.fire_level > 0.7:
        fire = "можно говорить довольно жёстко и прямо"
    elif profile.fire_level < 0.25:
        fire = "важна бережная, мягкая подача"
    else:
        fire = "честность окей, но без перегибов"

    return f"{addr}; {frm}; {struct}; {depth}; {fire}."


def describe_communication_style(telegram_id: int) -> str:
    """
    Описание того, как бот чувствует стиль пользователя.
    Использует StyleProfile; если его нет, даёт базовый эвристический текст.
    """
    profile = _load_style_profile(telegram_id)
    if profile:
        return style_profile_to_summary(profile)

    texts = get_recent_user_messages(telegram_id, limit=30)
    if not texts:
        return "Пока мало данных — подстраиваюсь под тебя по ходу диалога."

    joined = " ".join(texts)
    total_len = sum(len(t) for t in texts if t)
    avg_len = total_len / max(len(texts), 1)

    if avg_len < 80:
        length_desc = "короткие, ёмкие сообщения"
    elif avg_len < 220:
        length_desc = "средний объём без перегруза"
    else:
        length_desc = "развёрнутые, подробные сообщения"

    lower = joined.lower()
    formal_markers = ["здравствуйте", "добрый день", "добрый вечер", "уважаем", "будьте добры"]
    uses_vy = any(m in lower for m in formal_markers) or " вы " in lower
    tone_desc = (
        "общение на «Вы», аккуратный тон"
        if uses_vy
        else "общение на «ты», живой и прямой тон"
    )

    if any(ch in joined for ch in ["\n- ", "\n•", "1.", "2)"]):
        struct_desc = "любишь структуру и списки"
    else:
        struct_desc = "чаще используешь свободный формат без жёсткой структуры"

    return f"{length_desc}; {tone_desc}; {struct_desc}."


def build_style_hint(telegram_id: int) -> str:
    """
    Внешний интерфейс для LLM: обновляем StyleProfile и выдаём текстовую подсказку.
    """
    profile = build_style_profile_from_history(telegram_id)
    return style_profile_to_hint(profile)


# ---------------------------------------------------------------------------
# Реферальная система
# ---------------------------------------------------------------------------


def register_referral(inviter_id: int, invited_id: int) -> None:
    if inviter_id == invited_id:
        return

    now_ts = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO referrals (inviter_id, invited_id, created_at_ts)
        VALUES (?, ?, ?)
        """,
        (inviter_id, invited_id, now_ts),
    )
    conn.commit()
    conn.close()


def get_referral_stats(inviter_id: int) -> Tuple[int, int]:
    now_ts = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT COUNT(DISTINCT invited_id) AS cnt FROM referrals WHERE inviter_id = ?",
        (inviter_id,),
    )
    row = cur.fetchone()
    invited = int(row["cnt"] or 0) if row else 0

    cur.execute(
        """
        SELECT COUNT(DISTINCT r.invited_id) AS cnt
        FROM referrals r
        JOIN users_v2 u ON u.telegram_id = r.invited_id
        WHERE r.inviter_id = ?
          AND u.premium_until_ts IS NOT NULL
          AND u.premium_until_ts > ?
        """,
        (inviter_id, now_ts),
    )
    row = cur.fetchone()
    premium = int(row["cnt"] or 0) if row else 0

    conn.close()
    return invited, premium


# ---------------------------------------------------------------------------
# Учёт счетов Crypto Pay
# ---------------------------------------------------------------------------


def save_invoice_record(invoice: Dict[str, Any], plan_code: str, telegram_id: int) -> None:
    invoice_id = int(invoice["invoice_id"])
    status = str(invoice.get("status") or "active").lower()
    now_ts = int(time.time())

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO invoices (
            invoice_id,
            telegram_id,
            plan_code,
            status,
            created_at_ts,
            updated_at_ts
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (invoice_id, telegram_id, plan_code, status, now_ts, now_ts),
    )

    if cur.rowcount == 0:
        cur.execute(
            """
            UPDATE invoices
            SET telegram_id = ?, plan_code = ?, status = ?, updated_at_ts = ?
            WHERE invoice_id = ?
            """,
            (telegram_id, plan_code, status, now_ts, invoice_id),
        )

    conn.commit()
    conn.close()


def get_active_invoices(limit: int = 100) -> List[sqlite3.Row]:
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT invoice_id, telegram_id, plan_code, status
        FROM invoices
        WHERE status IN ('active', 'created')
        ORDER BY created_at_ts ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def update_invoice_status(invoice_id: int, status: str) -> None:
    now_ts = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE invoices
        SET status = ?, updated_at_ts = ?
        WHERE invoice_id = ?
        """,
        (status, now_ts, invoice_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Интенты, эмоциональный радар и промпты
# ---------------------------------------------------------------------------


def detect_intent(user_text: str) -> str:
    text = (user_text or "").lower()

    plan_keywords = ["план", "по шагам", "roadmap", "чек-лист", "чеклист", "структурируй"]
    if any(k in text for k in plan_keywords):
        return "plan"

    brainstorm_keywords = [
        "идеи",
        "варианты",
        "мозговой штурм",
        "brainstorm",
        "нейминг",
        "название",
        "как назвать",
    ]
    if any(k in text for k in brainstorm_keywords):
        return "brainstorm"

    emotional_keywords = [
        "мне плохо",
        "плохо на душе",
        "тревога",
        "тревожно",
        "страшно",
        "выгорел",
        "выгорание",
        "нет сил",
        "устал",
        "мотивация",
    ]
    if any(k in text for k in emotional_keywords):
        return "emotional"

    analysis_keywords = [
        "проанализируй",
        "анализ",
        "разбор",
        "почему",
        "объясни",
        "разложи",
    ]
    if any(k in text for k in analysis_keywords) or len(text) > 600:
        return "analysis"

    return "other"


# ---------- ЭМОЦИОНАЛЬНЫЙ РАДАР ----------


def detect_emotion(user_text: str) -> str:
    """
    Лёгкий детектор эмоционального состояния по сообщению.
    Возвращает один из тегов:
    'overload' / 'anxiety' / 'anger' / 'inspired' / 'apathy' / 'neutral'
    """
    text = (user_text or "").lower()

    anger_keys = ["злость", "злюсь", "бесит", "раздражает", "раздражение", "агресс", "кипит"]
    if any(k in text for k in anger_keys):
        return "anger"

    overload_keys = [
        "перегруз",
        "перегружен",
        "слишком много",
        "не успеваю",
        "завал",
        "голова не варит",
        "голова кипит",
        "давит",
        "давление задач",
    ]
    if any(k in text for k in overload_keys):
        return "overload"

    anxiety_keys = [
        "тревог",
        "пережива",
        "волнуюсь",
        "боюсь",
        "страшно",
        "нервнича",
        "паник",
    ]
    if any(k in text for k in anxiety_keys):
        return "anxiety"

    apathy_keys = [
        "нет сил",
        "ничего не хочется",
        "апат",
        "пусто внутри",
        "опустились руки",
        "устал жить",
        "выгорел",
        "выгорание",
        "устал до смерти",
    ]
    if any(k in text for k in apathy_keys):
        return "apathy"

    inspired_keys = [
        "вдохнов",
        "кайф",
        "заряжен",
        "огонь",
        "горю идеей",
        "мотивирован",
        "лютый заряд",
    ]
    if any(k in text for k in inspired_keys):
        return "inspired"

    return "neutral"


def build_emotion_hint(emotion: str) -> str:
    """
    Превращаем тег состояния в подсказку для LLM.
    Важно: не просим модель озвучивать диагноз/эмоцию пользователю.
    """
    if emotion == "overload":
        return (
            "Если в запросе чувствуется перегруз и ощущение завала задач, "
            "отвечай как «холодная голова»: помоги упростить и разгрузить. "
            "Дай 3–5 простых шагов, упорядочь хаос, убери лишние действия. "
            "Не пиши напрямую, что заметил перегруз — просто веди себя спокойнее и структурнее."
        )
    if emotion == "anxiety":
        return (
            "Если в запросе много тревоги или переживаний, отвечай особенно мягко и опорно. "
            "Избегай катастрофизации и страшных формулировок. "
            "Дай 2–4 понятных шага, которые снижают неопределённость. "
            "Можешь предложить очень короткую дыхательную или заземляющую практику (1–2 предложения), "
            "но как опцию, а не как приказ. Не пиши фразу вида «я вижу, что ты тревожишься»."
        )
    if emotion == "anger":
        return (
            "Если чувствуется злость или раздражение, не подливай масла в огонь и не обесценивай эмоции. "
            "Помоги перевести энергию в конструктив: предложи фокус на действиях и конкретных шагах. "
            "Тон — спокойный, без морализаторства и без прямых оценок личности."
        )
    if emotion == "apathy":
        return (
            "Если ощущается апатия или сильная усталость, не дави и не читай нотаций. "
            "Предложи 1–3 очень простых, реалистичных шага, которые дают минимальное движение вперёд "
            "и чувство контроля. Избегай фраз вида «нужно просто взять себя в руки»."
        )
    if emotion == "inspired":
        return (
            "Если пользователь звучит вдохновлённо и заряженно, не тормози его энтузиазм. "
            "Помоги упаковать энергию в понятный план и следующие шаги, чуть структурируй идеи. "
            "Тон может быть более живым и поддерживающим."
        )
    return ""  # neutral


def build_system_prompt(mode_key: str, intent: str, style_hint: Optional[str]) -> str:
    mode_cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    if intent == "plan":
        intent_suffix = (
            "Пользователь ожидает прежде всего чёткий план действий. "
            "Сделай поэтапный план с логичными блоками и краткими пояснениями."
        )
    elif intent == "analysis":
        intent_suffix = (
            "Пользователь просит глубокий разбор. "
            "Разбери ситуацию по шагам: контекст → ключевые факторы → варианты → вывод."
        )
    elif intent == "brainstorm":
        intent_suffix = (
            "Пользователь ждёт мозговой штурм. "
            "Предложи несколько разных подходов и вариантов, сгруппируй их и рядом с каждым "
            "дай короткий комментарий."
        )
    elif intent == "emotional":
        intent_suffix = (
            "Пользователь в эмоциональном запросе. "
            "Сначала аккуратно отзеркаль состояние (без грубых ярлыков), затем предложи простые, "
            "реалистичные шаги без токсичного позитива."
        )
    else:
        intent_suffix = (
            "Формат ответа выбирай исходя из запроса, но всегда держи структуру и ясность мысли."
        )

    parts = [BASE_SYSTEM_PROMPT, mode_cfg.system_suffix, intent_suffix]
    if style_hint:
        parts.append(style_hint)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# LLM: DeepSeek / Groq
# ---------------------------------------------------------------------------


async def _call_deepseek(
    user_text: str,
    mode_key: str,
    intent: str,
    style_hint: Optional[str],
    history: Optional[List[Dict[str, str]]],
) -> str:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")

    sys_prompt = build_system_prompt(mode_key, intent, style_hint)
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    messages: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    if history:
        messages.extend(history[-10:])
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
        raise RuntimeError(f"DeepSeek empty response: {data}")
    return (choices[0]["message"]["content"] or "").strip()


async def _call_groq(
    user_text: str,
    mode_key: str,
    intent: str,
    style_hint: Optional[str],
    history: Optional[List[Dict[str, str]]],
) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set")

    sys_prompt = build_system_prompt(mode_key, intent, style_hint)
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    messages: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    if history:
        messages.extend(history[-10:])
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
        raise RuntimeError(f"Groq empty response: {data}")
    return (choices[0]["message"]["content"] or "").strip()


async def generate_ai_reply(
    user_text: str,
    mode_key: str,
    style_hint: Optional[str] = None,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
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
            "⚠️ Что-то пошло не так при обращении к ИИ.\n"
            "Попробуй повторить запрос чуть позже."
        )

    return (
        "⚠️ ИИ-модель сейчас не настроена.\n"
        "Проверь конфигурацию сервера бота."
    )


# ---------------------------------------------------------------------------
# Crypto Pay API
# ---------------------------------------------------------------------------


async def crypto_create_invoice(plan: Plan, telegram_id: int) -> Optional[str]:
    if not CRYPTO_ENABLED:
        return None

    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}
    payload = {
        "asset": CRYPTO_DEFAULT_ASSET,
        "amount": f"{plan.price_usdt:.2f}",
        "description": f"BlackBox GPT — {plan.title}",
        "expires_in": 3600,
        "payload": json.dumps({"telegram_id": telegram_id, "plan": plan.code}),
    }

    async with httpx.AsyncClient(base_url=CRYPTO_PAY_API_URL, timeout=15) as client:
        resp = await client.post("/createInvoice", headers=headers, json=payload)

    data = resp.json()
    if not data.get("ok"):
        logger.error("CryptoPay createInvoice error: %s", data)
        return None

    invoice = data["result"]

    try:
        save_invoice_record(invoice, plan.code, telegram_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to save invoice record: %r", e)

    pay_url = (
        invoice.get("bot_invoice_url")
        or invoice.get("mini_app_invoice_url")
        or invoice.get("pay_url")
    )
    return pay_url


async def crypto_get_invoices(invoice_ids: List[int]) -> List[Dict[str, Any]]:
    if not CRYPTO_ENABLED or not invoice_ids:
        return []

    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}
    payload = {"invoice_ids": invoice_ids}

    async with httpx.AsyncClient(base_url=CRYPTO_PAY_API_URL, timeout=15) as client:
        resp = await client.post("/getInvoices", headers=headers, json=payload)

    data = resp.json()
    if not data.get("ok"):
        logger.error("CryptoPay getInvoices error: %s", data)
        return []

    result = data.get("result") or []
    return result if isinstance(result, list) else []


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
    user_row = await _ensure_user(message)
    username = message.from_user.username

    if is_user_admin(username):
        return True, user_row

    if user_is_premium(user_row):
        return True, user_row

    if not LLM_AVAILABLE:
        return True, user_row

    used = int(user_row["free_used"] or 0)
    if used >= FREE_MESSAGES_LIMIT:
        return False, user_row

    new_used = increment_free_used(user_row["telegram_id"])
    logger.info(
        "User %s used free message %s/%s",
        user_row["telegram_id"],
        new_used,
        FREE_MESSAGES_LIMIT,
    )
    return True, user_row


# ---------------------------------------------------------------------------
# UI: клавиатуры (таскбар)
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


# ---------------------------------------------------------------------------
# Живое печатание 2.0
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
# Router и хэндлеры
# ---------------------------------------------------------------------------

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    # /start или /start ref_123456789
    inviter_id: Optional[int] = None
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            arg = parts[1].strip()
            if arg.startswith("ref_"):
                try:
                    inviter_id = int(arg[4:])
                except ValueError:
                    inviter_id = None

    user_row = await _ensure_user(message)

    if inviter_id:
        register_referral(inviter_id, int(user_row["telegram_id"]))

    mode_key = get_user_mode_from_row(user_row)
    mode_cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    is_premium = user_is_premium(user_row)
    used = int(user_row["free_used"] or 0)
    left = max(FREE_MESSAGES_LIMIT - used, 0)

    if is_premium:
        status_line = (
            "Премиум-доступ <b>активен</b>: общение без лимитов и приоритетная скорость ответов."
        )
    else:
        status_line = (
            f"Сейчас у тебя базовый доступ. Доступно ≈ <b>{left}</b> бесплатных сообщений "
            f"из {FREE_MESSAGES_LIMIT}, дальше — через подписку."
        )

    name = message.from_user.first_name or "друг"

    text = (
        f"<b>Привет, {name}!</b>\n\n"
        "<b>BlackBox GPT</b> — универсальный ИИ-ассистент премиум-класса.\n"
        "Минимализм во всём: только диалог и нижний таскбар.\n\n"
        "<b>Как работать:</b>\n"
        "• просто задай первый вопрос — от медицины и бизнеса до личного развития;\n"
        "• или выбери режим внизу, если нужен особый фокус.\n\n"
        f"{status_line}\n\n"
        f"<b>Текущий режим:</b> {mode_cfg.title} — {mode_cfg.short_label}.\n\n"
        "Пиши, чем тебе помочь прямо сейчас."
    )

    await message.answer(text, reply_markup=main_taskbar())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "<b>Что умеет BlackBox GPT</b>\n\n"
        "• разбирать сложные ситуации и помогать принять решение;\n"
        "• собирать планы и чек-листы под твои задачи;\n"
        "• помогать с текстами, идеями, креативом и кодом;\n"
        "• аккуратно давать справочную информацию по медицине (но не ставить диагнозы).\n\n"
        "Выбирай режим внизу или просто пиши запрос — дальше можно общаться как с живым умным собеседником."
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
    set_user_mode(message.from_user.id, mode_key)
    cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    text = (
        f"<b>Режим установлен:</b> {cfg.title}.\n\n"
        f"{cfg.description}\n\n"
        "Можешь сразу писать следующий запрос — я отвечу в этом режиме."
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
    telegram_id = int(user_row["telegram_id"])

    stats = get_user_stats(telegram_id)
    style_desc = describe_communication_style(telegram_id)

    mode_key = get_user_mode_from_row(user_row)
    mode_cfg = MODE_CONFIGS.get(mode_key, MODE_CONFIGS[DEFAULT_MODE_KEY])

    is_premium = user_is_premium(user_row)
    used = stats["free_used"] or 0
    remaining = max(FREE_MESSAGES_LIMIT - used, 0)

    if is_premium:
        access_status = (
            "Премиум-доступ <b>активен</b> — можешь общаться без лимитов и очередей."
        )
    else:
        access_status = (
            "Базовый доступ.\n"
            f"Использовано <b>{used}</b> бесплатных сообщений из <b>{FREE_MESSAGES_LIMIT}</b>. "
            f"Осталось ≈ <b>{remaining}</b>."
        )

    created_line = ""
    if stats["created_at_ts"]:
        created_dt = time.strftime("%d.%m.%Y", time.localtime(stats["created_at_ts"]))
        created_line = f"\n<b>С BlackBox GPT с:</b> {created_dt}"

    messages_line = ""
    if stats["message_count"]:
        messages_line = f"\n<b>Сообщений в диалоге:</b> {stats['message_count']}"

    username = message.from_user.username or "без username"

    text = (
        "<b>Профиль BlackBox GPT</b>\n\n"
        f"<b>Аккаунт:</b> @{username}\n"
        f"<b>ID:</b> <code>{telegram_id}</code>\n"
        f"<b>Текущий режим:</b> {mode_cfg.title} — {mode_cfg.short_label}."
        f"{created_line}"
        f"{messages_line}\n\n"
        "<b>Статус доступа</b>\n"
        f"{access_status}\n\n"
        "<b>Как я тебя чувствую:</b>\n"
        f"• {style_desc}\n"
        "• бот подстраивает тон, длину и формат ответов под твой стиль общения.\n\n"
        "<i>Дальше сюда добавим цели, проекты и привычки — профиль станет "
        "личной панелью управления твоим ИИ-ассистентом.</i>"
    )

    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Рефералы --------------------------


@router.message(F.text == BTN_REFERRALS)
async def show_referrals(message: Message) -> None:
    user_row = await _ensure_user(message)
    telegram_id = int(user_row["telegram_id"])

    invited, premium = get_referral_stats(telegram_id)

    me = await message.bot.get_me()
    bot_username = me.username
    ref_link = f"https://t.me/{bot_username}?start=ref_{telegram_id}"

    text = (
        "<b>Реферальная система BlackBox GPT</b>\n\n"
        "У тебя есть персональная ссылка. Все, кто запускают бота по ней, "
        "закрепляются за твоим аккаунтом.\n\n"
        "<b>Твоя ссылка:</b>\n"
        f'<a href="{ref_link}">{ref_link}</a>\n\n'
        "<i>Нажми и удерживай, чтобы скопировать или сразу отправить друзьям.</i>\n\n"
        "<b>Твоя статистика:</b>\n"
        f"• приглашено пользователей: <b>{invited}</b>\n"
        f"• из них с активным премиумом: <b>{premium}</b>\n\n"
        "<i>В следующих обновлениях сюда добавим конкретные бонусы за приглашения: "
        "дополнительные запросы, закрытые режимы и особые инструменты.</i>"
    )

    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Подписка --------------------------


async def _subscription_overview_text(user_row: sqlite3.Row) -> str:
    is_premium = user_is_premium(user_row)
    used = int(user_row["free_used"] or 0)
    left = max(FREE_MESSAGES_LIMIT - used, 0)

    if is_premium:
        header = (
            "<b>Подписка BlackBox GPT</b>\n\n"
            "Премиум уже активен — продлить доступ можно в любой момент.\n\n"
        )
    else:
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
        "\nВыбери тариф на таскбаре ниже — я создам персональную ссылку на оплату в Crypto Bot."
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

    pay_url = await crypto_create_invoice(plan, message.from_user.id)
    if not pay_url:
        await message.answer(
            "Не удалось создать счёт. Попробуй позже.",
            reply_markup=subscription_taskbar(),
        )
        return

    text = (
        "<b>Оформление подписки BlackBox GPT</b>\n\n"
        f"<b>План:</b> {plan.title}\n"
        f"<b>Сумма:</b> {plan.price_usdt:.2f} USDT\n\n"
        "Ссылка на оплату через Crypto Bot:\n"
        f"{pay_url}\n\n"
        "Как только платёж пройдёт, бот автоматически активирует премиум-доступ "
        "и отправит тебе уведомление."
    )
    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Назад --------------------------


@router.message(F.text == BTN_BACK)
async def handle_back(message: Message) -> None:
    text = (
        "Возвращаю на главный экран.\n"
        "Снизу снова универсальный таскбар: режимы, профиль, подписка и рефералы."
    )
    await message.answer(text, reply_markup=main_taskbar())


# -------------------------- Админ-команда --------------------------


@router.message(Command("grant_premium"))
async def cmd_grant_premium(message: Message) -> None:
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


# -------------------------- Основной диалог --------------------------


@router.message(F.chat.type == ChatType.PRIVATE)
async def handle_private_chat(message: Message) -> None:
    if not message.text:
        return

    text = message.text.strip()
    if not text:
        return

    if text in MENU_TEXTS:
        return

    if text.startswith("/"):
        return

    allowed, user_row = await _check_access(message)
    if not allowed:
        used = get_free_used(message.from_user.id)
        msg = (
            "⚠️ Лимит бесплатных сообщений исчерпан.\n\n"
            f"Ты уже использовал {used} из {FREE_MESSAGES_LIMIT}.\n\n"
            "Чтобы продолжить общение без ограничений, открой раздел «💎 Подписка» "
            "и оформи премиум-доступ."
        )
        await message.answer(msg, reply_markup=main_taskbar())
        return

    telegram_id = message.from_user.id
    save_message(telegram_id, "user", text)

    # --- стиль + эмоции ---
    base_style_hint = build_style_hint(telegram_id)

    emotion = detect_emotion(text)
    emotion_hint = build_emotion_hint(emotion)
    if emotion_hint:
        style_hint = f"{base_style_hint}\n\n{emotion_hint}"
    else:
        style_hint = base_style_hint

    history = get_recent_dialog_history(telegram_id, limit=12)

    length = len(text)
    if length < 120:
        style_hint = (
            (style_hint + "\n\n") if style_hint else ""
        ) + (
            "Запрос короткий. Сделай ответ компактным (2–4 абзаца или список до 7 пунктов). "
            "В конце одной строкой предложи при необходимости «Раскрой подробнее»."
        )
        use_stream = False
    else:
        style_hint = (
            (style_hint + "\n\n") if style_hint else ""
        ) + (
            "Запрос объёмный. Дай глубокий, хорошо структурированный разбор с подзаголовками и выводом."
        )
        use_stream = True

    mode_key = get_user_mode_from_row(user_row)
    reply = await generate_ai_reply(
        user_text=text,
        mode_key=mode_key,
        style_hint=style_hint,
        history=history,
    )

    save_message(telegram_id, "assistant", reply)

    if use_stream:
        await stream_reply_text(message, reply)
    else:
        await message.answer(reply)


# ---------------------------------------------------------------------------
# Фоновый воркер для счетов
# ---------------------------------------------------------------------------


async def invoice_watcher(bot: Bot) -> None:
    if not CRYPTO_ENABLED:
        return

    logger.info("Invoice watcher started")

    while True:
        try:
            active = get_active_invoices(limit=100)
            if not active:
                await asyncio.sleep(30)
                continue

            invoice_ids = [int(r["invoice_id"]) for r in active]
            remote_list = await crypto_get_invoices(invoice_ids)
            remote_by_id = {int(inv["invoice_id"]): inv for inv in remote_list}

            for row in active:
                iid = int(row["invoice_id"])
                local_status = str(row["status"])
                inv = remote_by_id.get(iid)
                if not inv:
                    continue

                status_remote = str(inv.get("status") or "").lower()
                if status_remote == local_status:
                    continue

                update_invoice_status(iid, status_remote)

                tg_id = int(row["telegram_id"])
                plan_code = row["plan_code"]
                plan = PLANS.get(plan_code)

                if status_remote == "paid":
                    if plan:
                        grant_premium(tg_id, plan.months)

                    try:
                        await bot.send_message(
                            tg_id,
                            (
                                "<b>Подписка активирована</b>\n\n"
                                f"Платёж за план «{plan.title if plan else 'подписка'}» "
                                "успешно получен. Премиум-доступ уже включён."
                            ),
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.exception(
                            "Failed to send premium activation message: %r", e
                        )

                elif status_remote == "expired":
                    try:
                        await bot.send_message(
                            tg_id,
                            (
                                "Счёт на оплату подписки истёк.\n"
                                "Если хочешь продолжить — открой раздел «💎 Подписка» "
                                "и создай новый счёт."
                            ),
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.exception(
                            "Failed to send invoice expired message: %r", e
                        )

        except Exception as e:  # noqa: BLE001
            logger.exception("Invoice watcher loop error: %r", e)

        await asyncio.sleep(30)


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
    init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)

    await set_bot_commands(bot)

    if CRYPTO_ENABLED:
        asyncio.create_task(invoice_watcher(bot))

    logger.info("Starting BlackBox GPT bot polling…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
