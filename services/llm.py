import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import AsyncGenerator, Dict, List, Optional

import httpx

from bot.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    ASSISTANT_MODES,
    DEFAULT_MODE_KEY,
)


class IntentType(str, Enum):
    """Тип интента — какой формат ответа сейчас нужен пользователю."""

    PLAN = "plan"                   # Нужен план / чек-лист
    ANALYSIS = "analysis"           # Глубокий разбор / анализ
    BRAINSTORM = "brainstorm"       # Мозговой штурм / идеи
    COACH = "coach"                 # Наставник / коучинговые вопросы
    TASKS = "tasks"                 # Выделить задачи из текста
    QA = "qa"                       # Обычный вопрос-ответ
    EMOTIONAL_SUPPORT = "emotional_support"  # Эмоциональная поддержка / мотивация
    SMALLTALK = "smalltalk"         # Лёгкая болтовня
    OTHER = "other"                 # Всё остальное


@dataclass
class Intent:
    """Результат анализа интента."""

    type: IntentType


def analyze_intent(message_text: str, mode_key: Optional[str] = None) -> Intent:
    """
    Лёгкий интент-детектор первого уровня.

    Здесь без LLM (чтобы не тратить токены на каждый запрос),
    только эвристики по ключевым словам и чуть-чуть логики.
    При желании потом можно усложнить и звать отдельную модель.
    """
    if not message_text:
        return Intent(IntentType.OTHER)

    text = message_text.lower()

    # 1. Жёсткие паттерны по ключевым словам — планы / чек-листы
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
    if "задачи" in text and any(
        kw in text for kw in ("выдели", "сформулируй", "определи", "составь", "оформи")
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
        )
    ):
        return Intent(IntentType.EMOTIONAL_SUPPORT)

    # 7. Вопросы — базовый Q&A
    trimmed = text.strip()
    if "?" in trimmed or trimmed.startswith(
        ("почему", "как ", "что ", "зачем", "когда", "где ", "какой", "какая", "какие")
    ):
        return Intent(IntentType.QA)

    # 8. Лёгкий приоритет по режиму (если явных ключевых слов нет)
    if mode_key:
        mk = mode_key.lower()
        if any(x in mk for x in ("mentor", "coach", "nastav")):
            return Intent(IntentType.COACH)
        if any(x in mk for x in ("creative", "kreat", "creativ")):
            return Intent(IntentType.BRAINSTORM)

    # 9. По умолчанию — лёгкая беседа
    return Intent(IntentType.SMALLTALK)


def _apply_intent_to_prompt(user_prompt: str, intent: Intent) -> str:
    """
    Второй слой: заворачиваем запрос пользователя в нужный формат
    под конкретный интент. Снаружи поведение бота не ломаем,
    меняем только то, КАК мы формулируем задачу для LLM.
    """
    base = user_prompt.strip()

    if intent.type == IntentType.PLAN:
        return (
            "Пользователь просит помочь с планированием.\n"
            "Составь чёткий, по пунктам, реалистичный план действий.\n"
            "Формат: пронумерованный список шагов, без воды.\n\n"
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
            "3) Аккуратно наметь возможные векторы действий без давления.\n\n"
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
            "3) Дай 2–4 конкретных шага, что можно сделать уже сегодня, чтобы стало чуть легче.\n\n"
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


def _build_messages(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    """
    Собираем messages для Chat Completions:
    [system] + history + [user], добавляя лёгкий интент-слой поверх user_prompt.
    """
    mode_cfg: Dict[str, str] = ASSISTANT_MODES.get(
        mode_key,
        ASSISTANT_MODES[DEFAULT_MODE_KEY],
    )
    system_prompt = mode_cfg["system_prompt"]

    # Новый слой: определяем интент и заворачиваем запрос под нужный формат
    intent = analyze_intent(user_prompt, mode_key=mode_key)
    transformed_user_prompt = _apply_intent_to_prompt(user_prompt, intent)

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
    ]

    if history:
        # history — список словарей вида {"role": "user"/"assistant", "content": "..."}
        for msg in history:
            if msg.get("role") in {"user", "assistant"} and msg.get("content"):
                messages.append(
                    {"role": msg["role"], "content": msg["content"]},
                )

    messages.append({"role": "user", "content": transformed_user_prompt})
    return messages


async def ask_llm_stream(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> AsyncGenerator[str, None]:
    """
    Стриминговый вызов DeepSeek Chat Completions.
    Отдаёт куски текста по мере генерации (как async-генератор).

    ⚠️ ВАЖНО: сигнатура не менялась — всё, что вызывало эту функцию, продолжит работать.
    """
    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": _build_messages(mode_key, user_prompt, history),
        "temperature": 0.7,
        "top_p": 0.9,
        "stream": True,
    }

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue

                # Стандартный формат: "data: {json}"
                if not line.startswith("data:"):
                    continue

                data_str = line[len("data:") :].strip()
                if not data_str or data_str == "[DONE]":
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    logging.warning("Cannot decode LLM stream chunk: %s", data_str)
                    continue

                try:
                    delta = data["choices"][0]["delta"]
                    content = delta.get("content")
                    if content:
                        yield content
                except (KeyError, IndexError) as e:
                    logging.warning("Unexpected stream chunk format: %s | %s", e, data)
                    continue


async def ask_llm(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """
    Нестрмиминговая обёртка: собирает все чанки в один ответ.

    ⚠️ Сигнатура тоже не менялась, просто внутри теперь работает новый интент-движок.
    """
    chunks: List[str] = []
    async for chunk in ask_llm_stream(mode_key, user_prompt, history):
        chunks.append(chunk)

    answer = "".join(chunks).strip()
    if not answer:
        return "Произошла ошибка при обработке ответа модели. Попробуй ещё раз."
    return answer
