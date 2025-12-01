from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from groq import Groq

from .modes import build_system_prompt, DEFAULT_MODE_KEY
from .limits import check_rate_limit

logger = logging.getLogger(__name__)

# --- Model configuration ------------------------------------------------------------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Основная универсальная модель (по умолчанию — GPT-OSS 120B на Groq)
MODEL_PRIMARY = os.getenv("MODEL_PRIMARY", "openai/gpt-oss-120b")

# Быстрая и дешёвая модель для простых задач и саммари
MODEL_FAST = os.getenv("MODEL_FAST", "llama-3.1-8b-instant")

# Модель с усиленным reasoning (задачи, задачи по коду, сложный анализ)
MODEL_REASONING = os.getenv("MODEL_REASONING", "deepseek-r1-distill-llama-70b")

# Включать ли мультимодельный режим (несколько моделей для одного запроса)
MULTI_MODEL_ENABLED = os.getenv("MULTI_MODEL_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

_client: Optional[Groq] = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


class RateLimitError(Exception):
    """Выбрасывается при превышении лимита запросов для пользователя."""

    def __init__(self, retry_after: Optional[int], message: Optional[str]) -> None:
        self.retry_after = retry_after
        self.message = message or "Превышен лимит запросов."
        super().__init__(self.message)


@dataclass
class ConversationState:
    mode_key: str = DEFAULT_MODE_KEY
    messages: List[dict] = field(default_factory=list)


_conversations: Dict[int, ConversationState] = {}


def get_state(user_id: int) -> ConversationState:
    state = _conversations.get(user_id)
    if state is None:
        state = ConversationState()
        _conversations[user_id] = state
    return state


def reset_state(user_id: int) -> None:
    state = get_state(user_id)
    state.messages.clear()


def set_mode(user_id: int, mode_key: str) -> ConversationState:
    state = get_state(user_id)
    state.mode_key = mode_key or DEFAULT_MODE_KEY
    state.messages.clear()
    return state


# --- Текстовый пост-процессинг под Telegram (HTML) ----------------------------------------------


def _postprocess_reply(text: str) -> str:
    """
    Лёгкая чистка ответа под Telegram (HTML parse_mode):

    - убираем ```code fences```;
    - превращаем markdown-заголовки ##, ### в <b>...</b>;
    - убираем разделители таблиц типа |----|----|;
    - строки таблиц '| кол1 | кол2 |' превращаем в буллеты;
    - схлопываем >2 подряд пустые строки.

    HTML-теги (<b>, <i>, <u>, <a> и т.п.), которые вернула модель, не трогаем.
    """
    if not text:
        return text

    # 1) убрать ```code fences``` целиком
    text = re.sub(r"```.+?```", "", text, flags=re.S)

    # 2) заголовки вида "## Заголовок" -> пустая строка + <b>Заголовок</b>
    def _heading_repl(match: re.Match) -> str:
        title = match.group(1).strip()
        if not title:
            return ""
        return f"\n<b>{title}</b>\n"

    text = re.sub(r"^\s*#{2,6}\s+(.+)$", _heading_repl, text, flags=re.M)

    # 3) строки-разделители markdown-таблиц типа |----|----|
    text = re.sub(r"^\s*\|?\s*-{2,}\s*(\|-*)?\s*$", "", text, flags=re.M)

    # 4) строки таблиц '| ячейка1 | ячейка2 |' -> буллеты
    def _table_line_repl(match: re.Match) -> str:
        row = match.group(0).strip()
        cells = [c.strip() for c in row.strip("|").split("|")]
        cells = [c for c in cells if c]
        if not cells:
            return ""
        head, *rest = cells
        tail = (" — " + ", ".join(rest)) if rest else ""
        return f"• <b>{head}</b>{tail}\n"

    text = re.sub(r"^\s*\|.*\|\s*$", _table_line_repl, text, flags=re.M)

    # 5) убираем тройные и более пустые строки
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# --- Классификация запроса и выбор моделей ------------------------------------------------------


def _classify_intent(text: str, mode_key: str) -> Optional[str]:
    """
    Грубая классификация запроса, чтобы подмешивать шаблоны / выбирать модели.

    Возможные значения:
        - 'workout'    — программа тренировки
        - 'daily_plan' — план / распорядок дня
        - 'checklist'  — чек-лист / алгоритм шагов
        - 'content'    — работа с текстом / постами
        - None         — обычный ответ
    """
    t = text.lower()

    workout_markers = [
        "тренировка",
        "тренировочный план",
        "план тренировки",
        "программу тренировок",
        "программа тренировок",
        "сплит",
        "upper lower",
        "верх/низ",
        "зал",
        "спортзал",
        "тренажерный зал",
        "тренажёрный зал",
    ]
    for m in workout_markers:
        if m in t:
            return "workout"

    daily_plan_markers = [
        "план на день",
        "распорядок дня",
        "расписание на день",
        "режим дня",
        "структура дня",
        "как распределить день",
        "to-do на день",
        "лист дел на день",
        "список дел на день",
    ]
    for m in daily_plan_markers:
        if m in t:
            return "daily_plan"

    checklist_markers = [
        "чек-лист",
        "чек лист",
        "чеклист",
        "список шагов",
        "по шагам",
        "пошаговый план",
        "алгоритм действий",
        "что делать по шагам",
        "пошаговая инструкция",
    ]
    for m in checklist_markers:
        if m in t:
            return "checklist"

    content_markers = [
        "сделай пост",
        "подготовь пост",
        "сделай текст",
        "перепиши текст",
        "перепиши это",
        "оформи пост",
        "описание для канала",
        "описание профиля",
        "придумай название",
        "придумай заголовок",
        "сделай заголовок",
    ]
    for m in content_markers:
        if m in t:
            return "content"

    return None


def _is_reasoning_task(text: str) -> bool:
    """
    Очень грубое определение задач, где полезен reasoning-модель.
    """
    t = text.lower()
    reasoning_markers = [
        "реши задачу",
        "математика",
        "докажи",
        "обоснуй",
        "подробное объяснение",
        "step by step",
        "пошагово",
        "кейс",
        "разбор случая",
        "анализируй",
        "проанализируй",
        "напиши код",
        "ошибка в коде",
    ]
    if any(m in t for m in reasoning_markers):
        return True

    # длинный запрос с множеством знаков препинания — тоже кандидат
    if len(t) > 600 and (t.count("?") + t.count(".") + t.count("!")) > 5:
        return True

    return False


def _select_models_for_query(text: str, mode_key: str) -> List[str]:
    """
    Возвращает список ID моделей, которые стоит задействовать для одного запроса.
    """
    if not MULTI_MODEL_ENABLED:
        return [MODEL_PRIMARY]

    intent = _classify_intent(text, mode_key)
    reasoning = _is_reasoning_task(text)
    length = len(text)

    # Сценарий 1: сложный анализ / задачи / код -> reasoning + основной ответ
    if reasoning:
        return [MODEL_REASONING, MODEL_PRIMARY]

    # Сценарий 2: тренировки / план дня / чек-лист -> одна сильная универсальная модель
    if intent in {"workout", "daily_plan", "checklist"}:
        return [MODEL_PRIMARY]

    # Сценарий 3: короткие бытовые вопросы -> быстрая модель
    if length < 120:
        return [MODEL_FAST]

    # По умолчанию — одна основная модель
    return [MODEL_PRIMARY]


def _model_human_name(model_id: str) -> str:
    """
    Красивое имя модели для вывода в чат.
    """
    mapping = {
        "openai/gpt-oss-120b": "GPT-OSS 120B",
        "llama-3.1-8b-instant": "Llama 3.1 8B Instant",
        "llama-3.3-70b-versatile": "Llama 3.3 70B Versatile",
        "deepseek-r1-distill-llama-70b": "DeepSeek R1 Distill Llama 70B",
    }
    return mapping.get(model_id, model_id)


# --- Вызов LLM -------------------------------------------------------------------------------


async def _call_model(model_name: str, messages: List[dict]) -> str:
    if _client is None:
        raise RuntimeError("Groq client is not configured (GROQ_API_KEY is missing).")

    loop = asyncio.get_running_loop()

    def _do_request() -> str:
        completion = _client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.7,
            top_p=1,
            max_completion_tokens=2048,
        )
        return completion.choices[0].message.content or ""

    raw = await loop.run_in_executor(None, _do_request)
    return _postprocess_reply(raw)


def _trim_history(state: ConversationState, max_turns: int = 12) -> None:
    """
    Обрезает историю, оставляя не более max_turns последних пар сообщений user+assistant.
    """
    if len(state.messages) > max_turns * 2:
        state.messages = state.messages[-max_turns * 2 :]


# --- Публичный API для хендлеров --------------------------------------------------------------


async def ask_ai(user_id: int, text: str, user_name: Optional[str] = None) -> str:
    """
    Основная точка входа: один запрос пользователя -> один (или несколько) ответов моделей.
    """
    ok, retry_after, msg = check_rate_limit(user_id)
    if not ok:
        raise RateLimitError(retry_after=retry_after, message=msg)

    state = get_state(user_id)

    # Собираем system-промпт с учётом режима
    system_prompt = build_system_prompt(mode_key=state.mode_key, user_name=user_name)

    # Общий контекст для всех моделей: system + история + новый запрос
    messages: List[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(state.messages)
    messages.append({"role": "user", "content": text})

    models = _select_models_for_query(text, state.mode_key)

    try:
        if len(models) == 1:
            reply = await _call_model(models[0], messages)
        else:
            # Параллельный вызов нескольких моделей
            results = await asyncio.gather(
                *[_call_model(m, messages) for m in models],
                return_exceptions=True,
            )

            snippets: List[str] = []
            for idx, (model_name, result) in enumerate(zip(models, results)):
                if isinstance(result, Exception):
                    logger.exception("Error from model %s", model_name)
                    continue

                header_emoji = "🤖" if idx == 0 else "🧠"
                qualifier = "основной ответ" if idx == 0 else "альтернативный взгляд"
                header = f"{header_emoji} <b>{_model_human_name(model_name)} — {qualifier}</b>"
                snippets.append(header + "\n\n" + result.strip())

            if not snippets:
                raise RuntimeError("All model calls failed")

            separator = "\n\n━━━━━━━━━━━━━━\n\n"
            reply = separator.join(snippets)

    except Exception:
        logger.exception("Error while calling Groq ChatCompletion")
        raise

    # Обновляем историю диалога
    state.messages.append({"role": "user", "content": text})
    state.messages.append({"role": "assistant", "content": reply})
    _trim_history(state)

    return reply


async def healthcheck_llm() -> bool:
    """
    Лёгкий ping модели по основной конфигурации.
    """
    if _client is None:
        return False

    try:
        loop = asyncio.get_running_loop()

        def _do() -> bool:
            _client.chat.completions.create(
                model=MODEL_PRIMARY,
                messages=[{"role": "user", "content": "ping"}],
                max_completion_tokens=1,
                temperature=0.0,
            )
            return True

        return await loop.run_in_executor(None, _do)
    except Exception:
        logger.exception("LLM healthcheck failed")
        return False
