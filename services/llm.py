from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

# Важно: импортируем весь config целиком, чтобы не ловить ImportError
from bot import config as bot_config

logger = logging.getLogger(__name__)

# ==============================
#   Конфиг DeepSeek + режимы
# ==============================

DEEPSEEK_API_KEY: str = getattr(bot_config, "DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL: str = getattr(bot_config, "DEEPSEEK_MODEL", "deepseek-chat")

# Если в config нет явного URL — используем дефолтный эндпоинт DeepSeek
DEEPSEEK_API_URL: str = getattr(
    bot_config,
    "DEEPSEEK_API_URL",
    "https://api.deepseek.com/chat/completions",
)

ASSISTANT_MODES: Dict[str, Dict[str, Any]] = getattr(bot_config, "ASSISTANT_MODES", {})
DEFAULT_MODE_KEY: str = getattr(bot_config, "DEFAULT_MODE_KEY", "universal")


# ==============================
#   Интенты (двухслойный движок)
# ==============================

class IntentType(str, Enum):
    """Тип интента — в каком формате сейчас нужен ответ."""

    PLAN = "plan"                   # Нужен план / чек-лист
    ANALYSIS = "analysis"           # Глубокий разбор / анализ
    BRAINSTORM = "brainstorm"       # Мозговой штурм / идеи
    COACH = "coach"                 # Наставник / коучинговый формат
    TASKS = "tasks"                 # Выделить задачи из текста
    QA = "qa"                       # Обычный вопрос-ответ
    EMOTIONAL_SUPPORT = "emotional_support"  # Эмоциональная поддержка / мотивация
    SMALLTALK = "smalltalk"         # Лёгкая болтовня / общий разговор
    OTHER = "other"                 # Всё остальное


@dataclass
class Intent:
    """Результат анализа интента."""
    type: IntentType


def analyze_intent(message_text: str, mode_key: Optional[str] = None) -> Intent:
    """
    Лёгкий интент-детектор первого уровня.

    Здесь без отдельного LLM (чтобы не жечь токены на каждый запрос),
    только эвристики по ключевым словам + немного логики по режиму.

    Важно: интент влияет только на формулировку промпта к модели,
    внешний интерфейс бота не меняем.
    """
    if not message_text:
        return Intent(IntentType.OTHER)

    text = message_text.lower()

    # 1. Планы / чек-листы / роадмапы
    if any(
        kw in text
        for kw in (
            "план",
            "по шагам",
            "шаг за шагом",
            "roadmap",
            "дорожную карту",
            "чек-лист",
            "чек лист",
        )
    ):
        return Intent(IntentType.PLAN)

    # 2. Анализ / разбор
    if any(
        kw in text
        for kw in (
            "проанализируй",
            "анализируй",
            "анализ",
            "разбор",
            "разбери",
            "разложи по полочкам",
        )
    ):
        return Intent(IntentType.ANALYSIS)

    # 3. Мозговой штурм / идеи
    if any(
        kw in text
        for kw in (
            "мозговой штурм",
            "брейншторм",
            "идеи",
            "идею",
            "варианты",
            "вариантов",
            "что можно сделать",
            "придумай варианты",
        )
    ):
        return Intent(IntentType.BRAINSTORM)

    # 4. Коучинг / наставничество
    if any(
        kw in text
        for kw in (
            "коуч",
            "коучинг",
            "наставник",
            "как наставник",
            "зайди как наставник",
            "задай вопросы",
            "задать вопросы",
            "помоги разобраться во мне",
        )
    ):
        return Intent(IntentType.COACH)

    # 5. Выделение задач
    if "задач" in text and any(
        kw in text
        for kw in (
            "выдели",
            "сформулируй",
            "определи",
            "составь",
            "оформи",
            "разбей",
        )
    ):
        return Intent(IntentType.TASKS)

    # 6. Эмоциональная поддержка / мотивация
    if any(
        kw in text
        for kw in (
            "поддержи",
            "поддержка",
            "мне тяжело",
            "мне плохо",
            "мне хреново",
            "выгорел",
            "выгорание",
            "мотивируй",
            "мотивация",
            "дай мотивацию",
            "нет сил",
        )
    ):
        return Intent(IntentType.EMOTIONAL_SUPPORT)

    # 7. Вопросы — базовый Q&A
    trimmed = text.strip()
    if "?" in trimmed or trimmed.startswith(
        (
            "почему",
            "как ",
            "что ",
            "зачем",
            "когда",
            "где ",
            "какой",
            "какая",
            "какие",
        )
    ):
        return Intent(IntentType.QA)

    # 8. Лёгкий приоритет по режиму (если ключевых слов не нашли)
    if mode_key:
        mk = mode_key.lower()
        if "mentor" in mk or "nastav" in mk or "coach" in mk:
            return Intent(IntentType.COACH)
        if "creative" in mk or "kreat" in mk or "creativ" in mk:
            return Intent(IntentType.BRAINSTORM)

    # 9. По умолчанию — лёгкая беседа
    return Intent(IntentType.SMALLTALK)


def _apply_intent_to_prompt(
    user_prompt: str,
    intent: Intent,
) -> str:
    """
    Второй слой: заворачиваем запрос пользователя в нужный формат
    под конкретный интент. Для SMALLTALK/OTHER — не трогаем текст.
    """
    base = user_prompt.strip()

    if intent.type == IntentType.PLAN:
        return (
            "Пользователь просит помочь с планированием.\n"
            "Сделай чёткий, по шагам, реалистичный план действий.\n"
            "Формат: пронумерованный список конкретных шагов, без воды.\n\n"
            f"Контекст от пользователя:\n{base}"
        )

    if intent.type == IntentType.ANALYSIS:
        return (
            "Сделай глубокий разбор ситуации или вопроса пользователя.\n"
            "Структура ответа:\n"
            "1) Кратко сформулируй суть запроса.\n"
            "2) Разбери ключевые факторы и причины.\n"
            "3) Обозначь риски и типичные ошибки.\n"
            "4) Дай конкретные рекомендации по шагам.\n\n"
            f"Запрос пользователя:\n{base}"
        )

    if intent.type == IntentType.BRAINSTORM:
        return (
            "Сделай мозговой штурм по теме пользователя.\n"
            "Задача — предложить несколько разных вариантов и идей, "
            "от более очевидных к более нестандартным.\n"
            "Подавай мысли структурировано в виде списка с короткими пояснениями.\n\n"
            f"Тема для генерации идей:\n{base}"
        )

    if intent.type == IntentType.COACH:
        return (
            "Выступи как личный наставник и коуч.\n"
            "Цель — не просто дать совет, а помочь человеку самому увидеть решения.\n"
            "Структура ответа:\n"
            "1) Кратко отзеркаль состояние и запрос пользователя.\n"
            "2) Задай 3–7 точных вопросов для саморефлексии.\n"
            "3) Мягко наметь возможные векторы действий без давления.\n\n"
            f"Контекст от пользователя:\n{base}"
        )

    if intent.type == IntentType.TASKS:
        return (
            "Выдели из текста пользователя конкретные задачи, которые он может выполнить.\n"
            "Формат: чек-лист в виде списка пунктов, каждый пункт начинается с глагола.\n"
            "Если какие-то шаги зависят от решений пользователя — явно это укажи.\n\n"
            f"Текст пользователя:\n{base}"
        )

    if intent.type == IntentType.EMOTIONAL_SUPPORT:
        return (
            "Пользователь нуждается в эмоциональной поддержке и мотивации.\n"
            "Поговори по-человечески, без клише и токсичного позитива.\n"
            "Структура ответа:\n"
            "1) Признай и нормализуй его состояние и эмоции.\n"
            "2) Подсвети сильные стороны и ресурсы.\n"
            "3) Дай 2–4 конкретных шага, что можно сделать уже сегодня, "
            "чтобы стало чуть легче.\n\n"
            f"Запрос пользователя:\n{base}"
        )

    if intent.type == IntentType.QA:
        return (
            "Ответь на вопрос пользователя максимально ясно и структурированно.\n"
            "Если уместно — используй списки и поэтапные шаги, но избегай лишней воды.\n\n"
            f"Вопрос пользователя:\n{base}"
        )

    # SMALLTALK / OTHER — не заворачиваем, чтобы не ломать естественный диалог
    return base


# ==============================
#   Поведение режимов (коуч / бизнес / медицина)
# ==============================

@dataclass
class ModeBehavior:
    """Дополнительные правила для режимов."""
    system_suffix: str = ""   # доп. текст к системному промпту


MODE_BEHAVIORS: Dict[str, ModeBehavior] = {
    # 🔥 Наставник
    "mentor": ModeBehavior(
        system_suffix=(
            "\n\nРЕЖИМ: ЛИЧНЫЙ НАСТАВНИК.\n"
            "Всегда:\n"
            "- сначала кратко отражай суть запроса и состояние пользователя;\n"
            "- далее давай структурированный, но человеческий ответ;\n"
            "- в конце добавляй отдельный блок «Конкретные шаги» с 1–3 пунктами;\n"
            "- в самом конце задавай один уточняющий вопрос, чтобы углубить размышления.\n"
        )
    ),

    # 💼 Бизнес / «режим архитектора» для проектов
    "business": ModeBehavior(
        system_suffix=(
            "\n\nРЕЖИМ: БИЗНЕС-АРХИТЕКТОР.\n"
            "Фокус: цифры, гипотезы, риск/выгода, MVP, тестирование.\n"
            "Всегда:\n"
            "- формулируй чёткую бизнес-гипотезу или несколько вариантов;\n"
            "- используй язык экспериментов: что проверить, какие метрики смотреть;\n"
            "- добавляй блок «Что проверить» — 1–5 пунктов конкретных тестов;\n"
            "- по возможности давай ориентиры по цифрам (диапазоны, порядок величин).\n"
        )
    ),

    # 🩺 Медицина
    "medical": ModeBehavior(
        system_suffix=(
            "\n\nРЕЖИМ: ОСТОРОЖНЫЙ МЕДИЦИНСКИЙ АССИСТЕНТ.\n"
            "Строго соблюдай принципы безопасности.\n"
            "Всегда:\n"
            "- не ставь диагнозы и не замещай очную консультацию врача;\n"
            "- отвечай по структуре: «Кратко», «Возможные причины», "
            "«Что можно сделать», «Чего делать не стоит»;\n"
            "- подчёркивай необходимость обращения к врачу при тревожных симптомах;\n"
            "- в конце добавляй короткий дисклеймер о том, что информация не "
            "заменяет консультацию специалиста.\n"
        )
    ),
}


def _build_system_prompt(mode_key: str, style_hint: Optional[str]) -> str:
    """Собираем системный промпт с учётом режима и стиля."""
    mode_cfg: Dict[str, Any] = ASSISTANT_MODES.get(mode_key) or ASSISTANT_MODES.get(
        DEFAULT_MODE_KEY, {}
    )
    base_system_prompt: str = mode_cfg.get(
        "system_prompt",
        "Ты — умный универсальный ассистент. Отвечай структурировано и по делу.",
    )

    # Добавляем специальные правила для режима (коуч, бизнес, медицина и т.д.)
    behavior = MODE_BEHAVIORS.get(mode_key)
    if behavior and behavior.system_suffix:
        base_system_prompt = base_system_prompt.rstrip() + "\n" + behavior.system_suffix

    # Стиль общения пользователя (если где-то ядро его передаёт)
    if style_hint:
        base_system_prompt += (
            "\n\nДополнительно учитывай стиль общения пользователя:\n"
            f"{style_hint.strip()}"
        )

    return base_system_prompt


def _prepare_messages(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    style_hint: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Собираем messages для DeepSeek: system + history + user (через интент-слой)."""
    system_prompt = _build_system_prompt(mode_key, style_hint)

    # Новый слой: анализируем интент и трансформируем запрос
    intent = analyze_intent(user_prompt, mode_key=mode_key)
    transformed_user_prompt = _apply_intent_to_prompt(user_prompt, intent)

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
    ]

    # Если ядро где-то передаёт историю — аккуратно добавляем
    if history:
        for msg in history:
            role = msg.get("role")
            content = msg.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": transformed_user_prompt})
    return messages


# ==============================
#   Вызов DeepSeek
# ==============================

async def _call_deepseek(messages: List[Dict[str, str]]) -> str:
    """Один запрос к DeepSeek (без стриминга с их стороны)."""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY не задан в окружении.")

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": 0.7,
        "top_p": 0.9,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(DEEPSEEK_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        logger.exception("Unexpected DeepSeek response: %s", data)
        raise RuntimeError("Unexpected DeepSeek response structure")


def _estimate_tokens(*texts: str) -> int:
    """Грубая оценка числа токенов (чтобы не оставлять ноль)."""
    total_chars = sum(len(t or "") for t in texts)
    return max(1, total_chars // 4)


# ==============================
#   Публичный API: ask_llm_stream
# ==============================

async def _ask_llm_stream_impl(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    style_hint: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Внутренняя реализация стриминга.

    Каждый yield: {"delta": str, "full": str, "tokens": int}
    — это формат, который уже использует твой UI/хендлеры.
    """
    messages = _prepare_messages(
        mode_key=mode_key,
        user_prompt=user_prompt,
        history=history,
        style_hint=style_hint,
    )

    full_text = await _call_deepseek(messages)
    est_tokens = _estimate_tokens(user_prompt, full_text)

    # Имитация "живого" набора — режем на небольшие батчи по длине
    words = full_text.split()
    chunks: List[str] = []
    current = ""

    for w in words:
        candidate = (current + " " + w) if current else w
        if len(candidate) >= 120:
            chunks.append(candidate)
            current = ""
        else:
            current = candidate
    if current:
        chunks.append(current)

    assembled = ""
    if not chunks:
        # На всякий случай, если ответ пустой
        yield {"delta": "", "full": full_text, "tokens": est_tokens}
        return

    for chunk in chunks:
        assembled = (assembled + " " + chunk) if assembled else chunk
        yield {
            "delta": chunk,
            "full": assembled,
            "tokens": est_tokens,
        }
        # Небольшая пауза чтобы создать эффект печати
        await asyncio.sleep(0.05)


async def ask_llm_stream(
    *args,
    **kwargs,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Публичная функция, которую импортирует bot.main.

    Сделана максимально терпимой к разным старым вызовам:
    - позиционные аргументы: (mode_key, user_prompt) или (mode_key, user_prompt, history)
    - именованные: mode_key=..., user_prompt=..., history=..., style_hint=...

    Всегда возвращает async-генератор с dict:
      {"delta": str, "full": str, "tokens": int}
    """
    # Разбираем mode_key
    if "mode_key" in kwargs:
        mode_key = kwargs["mode_key"]
    elif len(args) >= 1:
        mode_key = args[0]
    else:
        mode_key = DEFAULT_MODE_KEY

    # Разбираем user_prompt
    if "user_prompt" in kwargs:
        user_prompt = kwargs["user_prompt"]
    elif len(args) >= 2:
        user_prompt = args[1]
    else:
        raise TypeError("ask_llm_stream: user_prompt is required")

    # История — только если явно передали
    history: Optional[List[Dict[str, str]]] = kwargs.get("history")
    if history is None and len(args) >= 3 and isinstance(args[2], list):
        history = args[2]

    # Стиль — только по имени (чтобы не путать с history)
    style_hint: Optional[str] = kwargs.get("style_hint")

    async for chunk in _ask_llm_stream_impl(
        mode_key=mode_key,
        user_prompt=user_prompt,
        history=history,
        style_hint=style_hint,
    ):
        yield chunk


async def ask_llm(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    style_hint: Optional[str] = None,
) -> str:
    """
    Нестриминговая обёртка: собирает полный ответ в один текст.
    Если нигде не используешь — можно и не трогать.
    """
    last_full = ""
    async for chunk in _ask_llm_stream_impl(
        mode_key=mode_key,
        user_prompt=user_prompt,
        history=history,
        style_hint=style_hint,
    ):
        last_full = chunk["full"]
    return last_full
