from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import AsyncGenerator, Dict, Any, List, Optional

import httpx

from bot.config import DEEPSEEK_API_KEY, DEEPSEEK_API_URL, DEEPSEEK_MODEL, ASSISTANT_MODES

logger = logging.getLogger(__name__)


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
        if "coach" in mk:
            return Intent(IntentType.COACH)
        if "creative" in mk or "creativ" in mk:
            return Intent(IntentType.BRAINSTORM)

    # 9. По умолчанию — лёгкая беседа
    return Intent(IntentType.SMALLTALK)


def _apply_intent_to_prompt(user_prompt: str, intent: Intent) -> str:
    """
    Второй слой: заворачиваем запрос пользователя в нужный формат
    под конкретный интент. Для SMALLTALK/OTHER — не трогаем текст.
    """
    base = user_prompt.strip()

    if intent.type == IntentType.PLAN:
        return (
            "Пользователь просит помочь с планированием.\n"
            "Составь чёткий, по шагам, реалистичный план действий.\n"
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
            "Твоя задача — предложить несколько разных вариантов и идей, "
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
#   Вызов DeepSeek
# ==============================

async def _call_deepseek(messages: List[Dict[str, str]]) -> str:
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "stream": False,  # стримим уже сами, разбивая готовый текст
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(DEEPSEEK_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    try:
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.exception("Unexpected DeepSeek response: %s", data)
        raise RuntimeError("Unexpected DeepSeek response") from e


def _estimate_tokens(*texts: str) -> int:
    """Небольшая грубая оценка токенов."""
    total_chars = sum(len(t or "") for t in texts)
    return max(1, total_chars // 4)


# ==============================
#   Публичный API: ask_llm_stream
# ==============================

async def ask_llm_stream(
    mode_key: str,
    user_prompt: str,
    style_hint: str | None = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Возвращает async-генератор с частями текста.
    Каждый yield: {"delta": str, "full": str, "tokens": int}

    ⚠️ Сигнатура и формат ответа не менялись — хендлеры и UI бота
    продолжают работать как раньше.

    Новое: перед отправкой в LLM мы определяем интент и
    слегка переструктурируем запрос, чтобы ответы были
    более осмысленными (план, разбор, коучинг, чек-лист и т.д.).
    """
    # Выбираем режим ассистента
    mode = ASSISTANT_MODES.get(mode_key) or ASSISTANT_MODES["universal"]
    system_prompt = mode["system_prompt"]

    # Учитываем стиль общения пользователя (как и было)
    if style_hint:
        system_prompt += (
            "\n\nДополнительно учитывай стиль общения пользователя:\n"
            f"{style_hint.strip()}"
        )

    # --- Новый слой: анализ интента ---
    intent = analyze_intent(user_prompt, mode_key=mode_key)
    transformed_user_prompt = _apply_intent_to_prompt(user_prompt, intent)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": transformed_user_prompt},
    ]

    # Получаем полный ответ от DeepSeek
    full_text = await _call_deepseek(messages)
    est_tokens = _estimate_tokens(user_prompt, full_text)

    # Плавное "живое" печатание — отдаём текст батчами
    words = full_text.split()
    chunks: List[str] = []
    current = ""

    for w in words:
        candidate = (current + " " + w) if current else w
        if len(candidate) >= 120:  # размер батча
            chunks.append(candidate)
            current = ""
        else:
            current = candidate

    if current:
        chunks.append(current)

    assembled = ""

    for chunk in chunks:
        assembled = (assembled + " " + chunk) if assembled else chunk
        yield {
            "delta": chunk,
            "full": assembled,
            "tokens": est_tokens,
        }
        # небольшая пауза, чтобы был эффект набора
        await asyncio.sleep(0.05)

    if not chunks:
        # На всякий случай, если текст оказался пустым
        yield {"delta": "", "full": full_text, "tokens": est_tokens}


# Необязательная удобная обёртка — если где-то захочешь просто получить текст
async def ask_llm(
    mode_key: str,
    user_prompt: str,
    style_hint: str | None = None,
) -> str:
    """
    Нестрмиминговая обёртка вокруг ask_llm_stream.
    Сейчас нигде не используется, но может пригодиться.
    """
    last_full = ""
    async for chunk in ask_llm_stream(mode_key=mode_key, user_prompt=user_prompt, style_hint=style_hint):
        last_full = chunk["full"]
    return last_full
