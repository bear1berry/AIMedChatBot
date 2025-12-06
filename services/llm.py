from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

from bot import config as bot_config

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY: str = getattr(bot_config, "DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL: str = getattr(bot_config, "DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_URL: str = getattr(
    bot_config,
    "DEEPSEEK_API_URL",
    "https://api.deepseek.com/chat/completions",
)

ASSISTANT_MODES: Dict[str, Dict[str, Any]] = getattr(bot_config, "ASSISTANT_MODES", {})
DEFAULT_MODE_KEY: str = getattr(bot_config, "DEFAULT_MODE_KEY", "universal")

# Чуть меньше лимита Telegram (4096), чтобы не упираться в ограничение
TELEGRAM_SAFE_LIMIT = 3800


class IntentType(str, Enum):
    PLAN = "plan"
    ANALYSIS = "analysis"
    BRAINSTORM = "brainstorm"
    COACH = "coach"
    TASKS = "tasks"
    QA = "qa"
    EMOTIONAL_SUPPORT = "emotional_support"
    SMALLTALK = "smalltalk"
    OTHER = "other"


@dataclass
class Intent:
    type: IntentType


def analyze_intent(message_text: str, mode_key: Optional[str] = None) -> Intent:
    if not message_text:
        return Intent(IntentType.OTHER)

    text = message_text.lower()

    # План / структура
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

    # Глубокий разбор
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

    # Мозговой штурм
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

    # Коучинг / наставник
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

    # Выделение задач
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

    # Эмоциональная поддержка
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

    # Вопрос-ответ
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

    # Лёгкий байас по режиму
    if mode_key:
        mk = mode_key.lower()
        if "mentor" in mk or "nastav" in mk or "coach" in mk:
            return Intent(IntentType.COACH)
        if "creative" in mk or "kreat" in mk or "creativ" in mk:
            return Intent(IntentType.BRAINSTORM)

    return Intent(IntentType.SMALLTALK)


def _apply_intent_to_prompt(user_prompt: str, intent: Intent) -> str:
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

    return base


@dataclass
class ModeBehavior:
    system_suffix: str = ""


MODE_BEHAVIORS: Dict[str, ModeBehavior] = {
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
    mode_cfg: Dict[str, Any] = ASSISTANT_MODES.get(mode_key) or ASSISTANT_MODES.get(
        DEFAULT_MODE_KEY, {}
    )
    base_system_prompt: str = mode_cfg.get(
        "system_prompt",
        "Ты — умный универсальный ассистент. Отвечай структурировано и по делу.",
    )

    behavior = MODE_BEHAVIORS.get(mode_key)
    if behavior and behavior.system_suffix:
        base_system_prompt = base_system_prompt.rstrip() + "\n" + behavior.system_suffix

    if style_hint:
        base_system_prompt += (
            "\n\nДополнительно учитывай стиль общения пользователя:\n"
            f"{style_hint.strip()}"
        )

    # Глобальное правило структуры (если не медрежим)
    if mode_key != "medical":
        base_system_prompt += (
            "\n\nСтруктурируй ответы: сначала короткий блок с ключевыми тезисами "
            "(3–7 пунктов), затем подробное раскрытие по пунктам."
        )

    return base_system_prompt


@dataclass
class AnswerChunk:
    kind: str  # outline / heading / paragraph / list
    text: str


@dataclass
class Answer:
    chunks: List[AnswerChunk]
    full_text: str
    has_more: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)


def _estimate_tokens(*texts: str) -> int:
    total_chars = sum(len(t or "") for t in texts)
    return max(1, total_chars // 4)


def _split_into_semantic_chunks(text: str) -> List[AnswerChunk]:
    text = text.strip()
    if not text:
        return []

    blocks = re.split(r"\n\s*\n", text)
    chunks: List[AnswerChunk] = []

    for i, block in enumerate(blocks):
        block = block.strip()
        if not block:
            continue

        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue

        first = lines[0].strip()

        # Outline / краткий блок
        if i == 0 and any(
            kw in first.lower()
            for kw in ("кратко", "тезисы", "итог", "оглавление")
        ):
            kind = "outline"
        # Списки
        elif all(
            ln.lstrip().startswith(("-", "•", "*", "—", "–"))
            or re.match(r"^\d+[\.\)]\s+", ln.lstrip())
            for ln in lines
        ):
            kind = "list"
        # Заголовки (короткая строка без точек)
        elif len(first) < 60 and not any(p in first for p in ".!?"):
            kind = "heading"
        else:
            kind = "paragraph"

        chunks.append(AnswerChunk(kind=kind, text=block))

    return chunks


def _prepare_messages(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    style_hint: Optional[str] = None,
    answer_mode: Optional[str] = None,  # "quick" | "deep"
) -> List[Dict[str, str]]:
    system_prompt = _build_system_prompt(mode_key, style_hint)

    intent = analyze_intent(user_prompt, mode_key=mode_key)
    transformed_user_prompt = _apply_intent_to_prompt(user_prompt, intent)

    # Быстрый / глубокий режим
    if answer_mode == "quick":
        transformed_user_prompt = (
            "РЕЖИМ ОТВЕТА: КОРОТКИЙ.\n"
            "Дай краткий, ёмкий ответ (до 3 абзацев, без большого количества деталей "
            "и длинных списков).\n\n"
            + transformed_user_prompt
        )
    elif answer_mode == "deep":
        transformed_user_prompt = (
            "РЕЖИМ ОТВЕТА: ГЛУБОКИЙ РАЗБОР.\n"
            "Сначала сделай блок «Кратко» — 3–7 тезисов (оглавление), "
            "далее подробный разбор по пунктам.\n\n"
            + transformed_user_prompt
        )

    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]

    if history:
        for msg in history:
            role = msg.get("role")
            content = msg.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": transformed_user_prompt})
    return messages


async def _call_deepseek(messages: List[Dict[str, str]]) -> str:
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


async def generate_answer(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    style_hint: Optional[str] = None,
    force_mode: Optional[str] = None,  # "quick" | "deep"
) -> Answer:
    # Определяем режим: быстрый / глубокий
    if force_mode in ("quick", "deep"):
        answer_mode = force_mode
    else:
        answer_mode = "quick" if len(user_prompt) < 160 else "deep"

    messages = _prepare_messages(
        mode_key=mode_key,
        user_prompt=user_prompt,
        history=history,
        style_hint=style_hint,
        answer_mode=answer_mode,
    )

    full_text = await _call_deepseek(messages)
    est_tokens = _estimate_tokens(user_prompt, full_text)

    visible_text = full_text
    has_more = False
    cut_offset = len(full_text)

    # Обрезаем, если упираемся в лимит Telegram
    if len(full_text) > TELEGRAM_SAFE_LIMIT:
        visible_text = full_text[:TELEGRAM_SAFE_LIMIT]
        last_break = visible_text.rfind("\n")
        if last_break > TELEGRAM_SAFE_LIMIT * 0.6:
            visible_text = visible_text[:last_break]
            cut_offset = last_break
        else:
            cut_offset = TELEGRAM_SAFE_LIMIT
        has_more = True

    chunks = _split_into_semantic_chunks(visible_text)
    meta: Dict[str, Any] = {
        "answer_mode": answer_mode,
        "tokens": est_tokens,
        "truncated": has_more,
        "cut_offset": cut_offset,
        "full_text": full_text,
        "can_expand": answer_mode == "quick",
    }

    return Answer(chunks=chunks, full_text=visible_text, has_more=has_more, meta=meta)


async def _ask_llm_stream_impl(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    style_hint: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Обёртка над generate_answer для старого интерфейса стриминга.
    """
    answer = await generate_answer(
        mode_key=mode_key,
        user_prompt=user_prompt,
        history=history,
        style_hint=style_hint,
    )

    assembled = ""
    sleep_base = 0.04 if answer.meta.get("answer_mode") == "deep" else 0.02

    for idx, ch in enumerate(answer.chunks):
        sep = "\n\n" if assembled else ""
        delta = sep + ch.text
        assembled = assembled + delta
        payload: Dict[str, Any] = {
            "delta": delta,
            "full": assembled,
            "tokens": answer.meta.get("tokens", 0),
            "kind": ch.kind,
        }
        if idx == len(answer.chunks) - 1:
            payload["has_more"] = answer.has_more
            payload["meta"] = answer.meta

        yield payload
        await asyncio.sleep(sleep_base)

    # На всякий случай, если чанков нет
    if not answer.chunks:
        yield {
            "delta": "",
            "full": answer.full_text,
            "tokens": answer.meta.get("tokens", 0),
            "has_more": answer.has_more,
            "meta": answer.meta,
        }


async def ask_llm_stream(
    *args,
    **kwargs,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Публичная функция стриминга (совместима с тем, как ты её вызывал раньше).
    """
    if "mode_key" in kwargs:
        mode_key = kwargs["mode_key"]
    elif len(args) >= 1:
        mode_key = args[0]
    else:
        mode_key = DEFAULT_MODE_KEY

    if "user_prompt" in kwargs:
        user_prompt = kwargs["user_prompt"]
    elif len(args) >= 2:
        user_prompt = args[1]
    else:
        raise TypeError("ask_llm_stream: user_prompt is required")

    history: Optional[List[Dict[str, str]]] = kwargs.get("history")
    if history is None and len(args) >= 3 and isinstance(args[2], list):
        history = args[2]

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
    Нестрочный вариант, просто вернёт финальный текст.
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
