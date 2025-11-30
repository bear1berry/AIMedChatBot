from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from groq import Groq

from .config import settings
from .limits import check_rate_limit
from .memory import load_conversation_row, save_conversation_row
from .modes import DEFAULT_MODE_KEY, build_system_prompt

logger = logging.getLogger(__name__)

if not settings.groq_api_key:
    logger.warning("GROQ_API_KEY is not set. LLM calls will fail.")

_client = Groq(api_key=settings.groq_api_key) if settings.groq_api_key else None


@dataclass
class ConversationState:
    mode_key: str = DEFAULT_MODE_KEY
    messages: List[dict] = field(default_factory=list)
    last_question: Optional[str] = None
    last_answer: Optional[str] = None
    verbosity: str = "normal"   # short / normal / long
    tone: str = "neutral"       # neutral / friendly / strict
    format_pref: str = "auto"   # auto / more_lists / more_text


_conversations: Dict[int, ConversationState] = {}


class RateLimitError(Exception):
    """Выбрасывается при превышении лимита запросов для пользователя."""

    def __init__(self, retry_after: Optional[int], message: Optional[str]) -> None:
        self.retry_after = retry_after
        self.message = message or "Превышен лимит запросов."
        super().__init__(self.message)


def _personalize_system_prompt(state: ConversationState, user_name: Optional[str]) -> str:
    base = build_system_prompt(mode_key=state.mode_key, user_name=user_name)
    extras: List[str] = []

    if state.verbosity == "short":
        extras.append("Отвечай максимально кратко: 3–5 предложений или 5–7 пунктов списка.")
    elif state.verbosity == "long":
        extras.append("Давай развёрнутые ответы с примерами и подробными пояснениями.")

    if state.tone == "friendly":
        extras.append(
            "Говори немного более дружелюбно и поддерживающе, можно немного эмодзи, но без фамильярности."
        )
    elif state.tone == "strict":
        extras.append("Стиль более официальный и лаконичный, без юмора и эмодзи.")

    if state.format_pref == "more_lists":
        extras.append("По возможности используй структурированные списки и подзаголовки.")
    elif state.format_pref == "more_text":
        extras.append("Используй больше связного текста, а списки только при необходимости.")

    if extras:
        base += "\n\nДополнительные настройки формата ответа:\n- " + "\n- ".join(extras)

    return base


def _state_from_row(row: Dict[str, object]) -> ConversationState:
    try:
        history = json.loads(row.get("history_json") or "[]")  # type: ignore[arg-type]
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []

    return ConversationState(
        mode_key=str(row.get("mode_key") or DEFAULT_MODE_KEY),
        messages=history,
        last_question=row.get("last_question"),  # type: ignore[arg-type]
        last_answer=row.get("last_answer"),      # type: ignore[arg-type]
        verbosity=str(row.get("verbosity") or "normal"),
        tone=str(row.get("tone") or "neutral"),
        format_pref=str(row.get("format_pref") or "auto"),
    )


def get_state(user_id: int) -> ConversationState:
    state = _conversations.get(user_id)
    if state is not None:
        return state

    row = load_conversation_row(user_id)
    if row is None:
        state = ConversationState()
    else:
        state = _state_from_row(row)

    _conversations[user_id] = state
    return state


def _save_state(user_id: int, state: ConversationState) -> None:
    save_conversation_row(
        user_id=user_id,
        mode_key=state.mode_key,
        history=state.messages,
        last_question=state.last_question,
        last_answer=state.last_answer,
        verbosity=state.verbosity,
        tone=state.tone,
        format_pref=state.format_pref,
    )


def set_mode(user_id: int, mode_key: str) -> ConversationState:
    state = get_state(user_id)
    state.mode_key = mode_key
    state.messages.clear()
    state.last_question = None
    state.last_answer = None
    _save_state(user_id, state)
    return state


def reset_state(user_id: int) -> None:
    state = get_state(user_id)
    state.messages.clear()
    state.last_question = None
    state.last_answer = None
    _save_state(user_id, state)


def update_preferences(
    user_id: int,
    verbosity: Optional[str] = None,
    tone: Optional[str] = None,
    format_pref: Optional[str] = None,
) -> ConversationState:
    state = get_state(user_id)

    if verbosity in {"short", "normal", "long"}:
        state.verbosity = verbosity
    if tone in {"neutral", "friendly", "strict"}:
        state.tone = tone
    if format_pref in {"auto", "more_lists", "more_text"}:
        state.format_pref = format_pref

    _save_state(user_id, state)
    return state


async def _call_llm(messages: List[dict]) -> str:
    if _client is None:
        raise RuntimeError("Groq client is not configured (no API key).")

    loop = asyncio.get_running_loop()

    def _do_request() -> str:
        completion = _client.chat.completions.create(
            model=settings.groq_chat_model,
            messages=messages,
            temperature=0.7,
            top_p=1,
            max_completion_tokens=2048,
            reasoning_effort="medium",
        )
        return completion.choices[0].message.content or ""

    return await loop.run_in_executor(None, _do_request)


def _trim_history(state: ConversationState, max_turns: int = 12) -> None:
    if len(state.messages) > max_turns * 2:
        state.messages = state.messages[-max_turns * 2 :]


async def ask_ai(user_id: int, text: str, user_name: Optional[str] = None) -> str:
    """
    Основная точка входа для обычных сообщений пользователя.
    """
    ok, retry_after, msg = check_rate_limit(user_id)
    if not ok:
        raise RateLimitError(retry_after=retry_after, message=msg)

    state = get_state(user_id)
    system_prompt = _personalize_system_prompt(state, user_name)

    messages: List[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(state.messages)
    messages.append({"role": "user", "content": text})

    try:
        reply = await _call_llm(messages)
    except Exception:
        logger.exception("Error while calling Groq ChatCompletion")
        raise

    state.messages.append({"role": "user", "content": text})
    state.messages.append({"role": "assistant", "content": reply})
    state.last_question = text
    state.last_answer = reply
    _trim_history(state)
    _save_state(user_id, state)

    return reply


async def continue_answer(user_id: int, user_name: Optional[str] = None) -> str:
    """
    Просит модель продолжить предыдущий ответ, не повторяя уже написанное.
    """
    state = get_state(user_id)
    if not state.last_answer:
        raise ValueError("Нет предыдущего ответа, который можно продолжить.")

    ok, retry_after, msg = check_rate_limit(user_id)
    if not ok:
        raise RateLimitError(retry_after=retry_after, message=msg)

    system_prompt = _personalize_system_prompt(state, user_name)
    messages: List[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(state.messages)
    continuation_request = (
        "Пожалуйста, продолжи свой предыдущий ответ, не повторяя уже написанное. "
        "Начни с того места, где мысль оборвалась."
    )
    messages.append({"role": "user", "content": continuation_request})

    try:
        reply = await _call_llm(messages)
    except Exception:
        logger.exception("Error while calling Groq ChatCompletion (continue_answer)")
        raise

    state.messages.append({"role": "user", "content": continuation_request})
    state.messages.append({"role": "assistant", "content": reply})
    state.last_answer = (state.last_answer or "") + "\n\n" + reply
    state.last_question = continuation_request
    _trim_history(state)
    _save_state(user_id, state)

    return reply


async def transform_last_answer(
    user_id: int,
    user_name: Optional[str],
    kind: str,
) -> str:
    """
    kind:
        - 'summary'  -> краткий конспект
        - 'post'     -> пост для Telegram-канала
        - 'patient'  -> проще для пациента
    """
    state = get_state(user_id)
    if not state.last_answer:
        raise ValueError("Пока нечего обрабатывать — сначала задай вопрос и получи ответ.")

    ok, retry_after, msg = check_rate_limit(user_id)
    if not ok:
        raise RateLimitError(retry_after=retry_after, message=msg)

    system_prompt = _personalize_system_prompt(state, user_name)
    messages: List[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(state.messages)

    if kind == "summary":
        instr = (
            "Сделай структурированный конспект моего предыдущего ответа. "
            "Кратко, по пунктам, с чёткими подзаголовками."
        )
    elif kind == "post":
        instr = (
            "На основе моего предыдущего ответа сделай готовый пост для Telegram-канала "
            "\"AI Medicine Daily\":\n"
            "- мощный цепляющий заголовок,\n"
            "- 3–6 абзацев основного текста,\n"
            "- аккуратный призыв к действию,\n"
            "- 3–7 уместных хештегов."
        )
    elif kind == "patient":
        instr = (
            "Объясни содержание моего предыдущего ответа максимально понятным языком для пациента, "
            "без сложной терминологии. Стиль спокойный и поддерживающий."
        )
    else:
        raise ValueError(f"Unknown transform kind: {kind}")

    user_msg = f"Вот предыдущий ответ ассистента:\n\n{state.last_answer}\n\n{instr}"
    messages.append({"role": "user", "content": user_msg})

    try:
        reply = await _call_llm(messages)
    except Exception:
        logger.exception("Error while calling Groq ChatCompletion (transform_last_answer)")
        raise

    state.messages.append({"role": "user", "content": instr})
    state.messages.append({"role": "assistant", "content": reply})
    state.last_question = instr
    state.last_answer = reply
    _trim_history(state)
    _save_state(user_id, state)

    return reply


async def healthcheck_llm() -> bool:
    """
    Лёгкий ping модели.
    """
    if _client is None:
        return False

    try:
        loop = asyncio.get_running_loop()

        def _do() -> bool:
            _client.chat.completions.create(
                model=settings.groq_chat_model,
                messages=[{"role": "user", "content": "ping"}],
                max_completion_tokens=1,
                temperature=0.0,
            )
            return True

        return await loop.run_in_executor(None, _do)
    except Exception:
        logger.exception("LLM healthcheck failed")
        return False
