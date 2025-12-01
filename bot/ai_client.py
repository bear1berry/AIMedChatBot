from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List

from groq import AsyncGroq

from .config import settings
from .modes import DEFAULT_MODE_KEY, build_system_prompt, get_mode_label

logger = logging.getLogger(__name__)


@dataclass
class ConversationState:
    mode_key: str = DEFAULT_MODE_KEY
    # Только история user/assistant, system добавляем каждый раз отдельно
    messages: List[Dict[str, str]] = field(default_factory=list)


_STATES: Dict[int, ConversationState] = {}

# Async-клиент Groq
_client = AsyncGroq(api_key=settings.groq_api_key)


def get_state(user_id: int) -> ConversationState:
    state = _STATES.get(user_id)
    if state is None:
        state = ConversationState()
        _STATES[user_id] = state
    return state


def reset_state(user_id: int) -> None:
    if user_id in _STATES:
        _STATES[user_id] = ConversationState(mode_key=DEFAULT_MODE_KEY)


def set_mode(user_id: int, mode_key: str) -> None:
    state = get_state(user_id)
    state.mode_key = mode_key
    # При смене режима лучше сбросить историю, чтобы стиль не мешался
    state.messages.clear()
    logger.info("User %s switched mode to %s", user_id, get_mode_label(mode_key))


async def healthcheck_llm() -> bool:
    """
    Простая проверка доступности модели.
    Не обязательно вызывать в бою, но оставим для /ping.
    """
    try:
        _ = await _client.chat.completions.create(
            model=settings.model_name,
            messages=[
                {"role": "system", "content": "You are a healthcheck probe."},
                {"role": "user", "content": "ping"},
            ],
            max_completion_tokens=1,
            temperature=0.0,
        )
        return True
    except Exception:
        logger.exception("Healthcheck failed")
        return False


def _postprocess_reply(text: str) -> str:
    """
    Приводим ответ модели к аккуратному Markdown без сырого HTML и мусора.
    """
    if not text:
        return "Извини, сейчас не получилось сформировать ответ."

    # Нормализуем переводы строк
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Убираем пробелы по краям
    text = text.strip()

    # 1) Конвертация HTML-жирного в Markdown: <b>...</b>, <strong>...</strong> → *...*
    def repl_bold(match: re.Match) -> str:
        inner = match.group(1).strip()
        if not inner:
            return ""
        return f"*{inner}*"

    text = re.sub(
        r"<\s*(?:b|strong)\s*>(.*?)<\s*/\s*(?:b|strong)\s*>",
        repl_bold,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # 2) Курсив: <i>...</i>, <em>...</em> → _..._
    def repl_italic(match: re.Match) -> str:
        inner = match.group(1).strip()
        if not inner:
            return ""
        return f"_{inner}_"

    text = re.sub(
        r"<\s*(?:i|em)\s*>(.*?)<\s*/\s*(?:i|em)\s*>",
        repl_italic,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # 3) Простые HTML-теги абзацев/переносов → перевод строки
    text = re.sub(
        r"</?(?:p|br|div|span)\s*/?>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )

    # 4) Остальные одиночные теги просто выбрасываем
    text = re.sub(r"</?[^>\n]{1,20}>", "", text)

    # 5) Заголовки вида "# Текст" / "## Текст" / "### Текст" → *Текст*
    def heading_repl(match: re.Match) -> str:
        title = match.group("title").strip()
        if not title:
            return ""
        return f"*{title}*\n"

    text = re.sub(
        r"^(?P<hashes>#{1,3})\s+(?P<title>.+)$",
        heading_repl,
        text,
        flags=re.MULTILINE,
    )

    # 6) Сжимаем пачки пустых строк
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


async def ask_ai(user_id: int, user_text: str) -> str:
    """
    Основной вызов модели Groq (openai/gpt-oss-120b) с учётом режима и истории.
    """
    state = get_state(user_id)
    mode_key = state.mode_key or DEFAULT_MODE_KEY

    system_prompt = build_system_prompt(mode_key)

    # Историю ограничиваем, чтобы не раздувать запрос.
    history = state.messages[-16:]  # примерно 8 последних обменов

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": user_text},
    ]

    try:
        completion = await _client.chat.completions.create(
            model=settings.model_name,
            messages=messages,
            temperature=0.35,
            max_completion_tokens=1400,
        )
    except Exception as e:
        logger.exception("Groq API error: %s", e)
        raise

    reply = completion.choices[0].message.content if completion.choices else ""
    reply = reply or "Извини, я не смог сформировать осмысленный ответ."

    reply = _postprocess_reply(reply)

    # Обновляем историю
    state.messages.append({"role": "user", "content": user_text})
    state.messages.append({"role": "assistant", "content": reply})

    return reply
