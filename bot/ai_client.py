from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

from .modes import build_system_prompt, DEFAULT_MODE_KEY
from .limits import check_rate_limit

logger = logging.getLogger(__name__)

# --- AIMLAPI CONFIG ---------------------------------------------------------------------------

AIML_API_KEY = os.getenv("AIML_API_KEY", "").strip()
AIML_API_BASE = os.getenv("AIML_API_BASE", "https://api.aimlapi.com/v1").rstrip("/")

# Базовые модели (можно переопределить через переменные окружения)
AIML_MODEL_PRIMARY = os.getenv("AIML_MODEL_PRIMARY", "openai/gpt-4.1-2025-04-14")
AIML_MODEL_FAST = os.getenv("AIML_MODEL_FAST", "openai/gpt-4.1-mini-2025-04-14")
AIML_MODEL_REASONING = os.getenv(
    "AIML_MODEL_REASONING",
    "deepseek/deepseek-reasoner",
)
AIML_MODEL_GPT_OSS_120B = os.getenv(
    "AIML_MODEL_GPT_OSS_120B",
    "openai/gpt-oss-120b",
)
AIML_MODEL_DEEPSEEK_CHAT = os.getenv(
    "AIML_MODEL_DEEPSEEK_CHAT",
    "deepseek/deepseek-chat-v3.1",
)


class RateLimitError(Exception):
    """Ошибка превышения лимита запросов для пользователя."""

    def __init__(self, retry_after: Optional[int], message: Optional[str]) -> None:
        self.retry_after = retry_after
        self.message = message or "Превышен лимит запросов."
        super().__init__(self.message)


@dataclass
class ConversationState:
    mode_key: str = DEFAULT_MODE_KEY
    messages: List[dict] = field(default_factory=list)
    # "auto" – автоматический выбор модели; остальные – режимы «чисто одна модель»
    model_profile: str = "auto"


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


# --- Профили выбора модели (режим «чисто одна модель») ----------------------------------------

_MODEL_PROFILE_LABELS: Dict[str, str] = {
    "auto": "Авто (выбор под задачу)",
    "gpt4.1": "GPT-4.1 (AIMLAPI)",
    "gpt4.1mini": "GPT-4.1 Mini (AIMLAPI)",
    "gpt_oss_120b": "GPT-OSS 120B (AIMLAPI)",
    "deepseek_reasoner": "DeepSeek Reasoner (AIMLAPI)",
    "deepseek_chat": "DeepSeek Chat (AIMLAPI)",
}


def set_model_profile(user_id: int, profile: str) -> ConversationState:
    """
    Режимы:
        - "auto"
        - "gpt4.1"
        - "gpt4.1mini"
        - "gpt_oss_120b"
        - "deepseek_reasoner"
        - "deepseek_chat"
    """
    if profile not in _MODEL_PROFILE_LABELS:
        profile = "auto"

    state = get_state(user_id)
    state.model_profile = profile
    # при смене модели — начинаем диалог с чистого листа
    state.messages.clear()
    return state


def get_model_profile_label(profile: str) -> str:
    return _MODEL_PROFILE_LABELS.get(profile, _MODEL_PROFILE_LABELS["auto"])


# --- Текстовый пост-процессинг под Telegram (HTML) ---------------------------------------------


def _postprocess_reply(text: str) -> str:
    """
    Лёгкая чистка ответа под Telegram (HTML parse_mode):

    - убираем ```code fences```;
    - превращаем markdown-заголовки ##, ### в <b>...</b>;
    - убираем разделители таблиц типа |----|----|;
    - строки таблиц '| кол1 | кол2 |' превращаем в буллеты;
    - схлопываем >2 подряд пустые строки.
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
    text = re.sub(
        r"^\s*\|?\s*-{2,}\s*(\|-*)?\s*$",
        "",
        text,
        flags=re.M,
    )

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
    Грубая классификация запроса, чтобы выбирать модель/шаблон.
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
    if any(m in t for m in workout_markers):
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
    if any(m in t for m in daily_plan_markers):
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
    if any(m in t for m in checklist_markers):
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
    if any(m in t for m in content_markers):
        return "content"

    return None


def _is_reasoning_task(text: str) -> bool:
    """
    Очень грубое определение задач, где полезна reasoning-модель.
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


def _select_models_for_query(text: str, state: ConversationState) -> List[str]:
    """
    Возвращает список ID моделей AIMLAPI, которые стоит задействовать для одного запроса.
    Учитывает выбранный пользователем профиль (model_profile).
    """
    profile = state.model_profile

    # Режимы «чисто одна модель»
    if profile == "gpt4.1":
        return [AIML_MODEL_PRIMARY]
    if profile == "gpt4.1mini":
        return [AIML_MODEL_FAST]
    if profile == "gpt_oss_120b":
        return [AIML_MODEL_GPT_OSS_120B]
    if profile == "deepseek_reasoner":
        return [AIML_MODEL_REASONING]
    if profile == "deepseek_chat":
        return [AIML_MODEL_DEEPSEEK_CHAT]

    # Авто-режим
    intent = _classify_intent(text, state.mode_key)
    reasoning = _is_reasoning_task(text)
    length = len(text)

    # Сложные задачи / код — reasoning + основной ответ
    if reasoning:
        return [AIML_MODEL_REASONING, AIML_MODEL_PRIMARY]

    # Планы, чек-листы
    if intent in {"workout", "daily_plan", "checklist"}:
        return [AIML_MODEL_PRIMARY]

    # Короткие бытовые вопросы
    if length < 120:
        return [AIML_MODEL_FAST]

    # По умолчанию — одна основная модель
    return [AIML_MODEL_PRIMARY]


def _model_human_name(model_id: str) -> str:
    """
    Красивое имя модели для вывода в чат.
    """
    mapping = {
        AIML_MODEL_PRIMARY: "GPT-4.1 (AIMLAPI)",
        AIML_MODEL_FAST: "GPT-4.1 Mini (AIMLAPI)",
        AIML_MODEL_REASONING: "DeepSeek Reasoner (AIMLAPI)",
        AIML_MODEL_GPT_OSS_120B: "GPT-OSS 120B (AIMLAPI)",
        AIML_MODEL_DEEPSEEK_CHAT: "DeepSeek Chat (AIMLAPI)",
        "deepseek/deepseek-r1": "DeepSeek Reasoner (AIMLAPI)",
        "deepseek/deepseek-chat": "DeepSeek Chat (AIMLAPI)",
        "openai/gpt-4.1-2025-04-14": "GPT-4.1 (AIMLAPI)",
        "openai/gpt-4.1-mini-2025-04-14": "GPT-4.1 Mini (AIMLAPI)",
        "openai/gpt-oss-120b": "GPT-OSS 120B (AIMLAPI)",
    }
    return mapping.get(model_id, model_id)


# --- Вызов AIMLAPI ----------------------------------------------------------------------------


async def _call_model(model_name: str, messages: List[dict]) -> str:
    """
    Общий вызов AIMLAPI /v1/chat/completions (OpenAI-совместимый).
    """
    if not AIML_API_KEY:
        raise RuntimeError("AIML_API_KEY не установлен в переменных окружения.")

    url = f"{AIML_API_BASE}/chat/completions"
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.7,
        "top_p": 1.0,
        "max_tokens": 2048,
    }
    headers = {
        "Authorization": f"Bearer {AIML_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Неверный формат ответа AIMLAPI: {data!r}")
    return _postprocess_reply(content or "")


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

    models = _select_models_for_query(text, state)

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
                snippets.append(header + "\n\n" + str(result).strip())

            if not snippets:
                raise RuntimeError("Все вызовы моделей завершились ошибкой.")

            separator = "\n\n━━━━━━━━━━━━━━\n\n"
            reply = separator.join(snippets)

    except Exception:
        logger.exception("Error while calling AIMLAPI ChatCompletion")
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
    if not AIML_API_KEY:
        return False

    url = f"{AIML_API_BASE}/chat/completions"
    payload = {
        "model": AIML_MODEL_FAST or AIML_MODEL_PRIMARY,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0.0,
    }
    headers = {
        "Authorization": f"Bearer {AIML_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("LLM healthcheck failed")
        return False
