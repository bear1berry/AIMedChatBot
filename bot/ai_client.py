from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from groq import Groq

from .config import settings
from .limits import check_rate_limit
from .memory import load_conversation_row, save_conversation_row
from .modes import DEFAULT_MODE_KEY, build_system_prompt

logger = logging.getLogger(__name__)


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


def _classify_intent(text: str, mode_key: str) -> Optional[str]:
    """
    Грубая классификация запроса, чтобы подмешивать шаблоны.

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


@dataclass
class ConversationState:
    mode_key: str = DEFAULT_MODE_KEY
    messages: List[dict] = field(default_factory=list)
    last_question: Optional[str] = None
    last_answer: Optional[str] = None
    verbosity: str = "normal"  # short / normal / long
    tone: str = "neutral"  # neutral / friendly / strict
    format_pref: str = "auto"  # auto / more_lists / more_text


class RateLimitError(Exception):
    """Выбрасывается при превышении лимита запросов для пользователя."""

    def __init__(self, retry_after: Optional[int], message: Optional[str]) -> None:
        self.retry_after = retry_after
        self.message = message or "Превышен лимит запросов."
        super().__init__(self.message)


_conversations: Dict[int, ConversationState] = {}

_client = Groq(api_key=settings.groq_api_key) if settings.groq_api_key else None


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
        last_answer=row.get("last_answer"),  # type: ignore[arg-type]
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


def _personalize_system_prompt(
    state: ConversationState,
    user_name: Optional[str],
    intent: Optional[str] = None,
) -> str:
    """
    Собирает финальный system prompt с учётом:
    - режима,
    - настроек пользователя (длина, тон, формат),
    - типа задачи (контент, тренировка, план дня, чек-лист).
    """
    base = build_system_prompt(mode_key=state.mode_key, user_name=user_name)
    extras: List[str] = []

    # Настройки формата
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

    # Инструкции под тип задачи
    if intent == "content":
        extras.append(
            "Сейчас пользователь просит помочь с созданием или редактированием текста/контента. "
            "Сконцентрируйся на структуре, читабельности и лаконичности, избегай лишней воды."
        )
    elif intent == "workout":
        extras.append(
            "Пользователь просит составить программу тренировки. "
            "Оформи ответ в телеграм-формате с блоками:\n"
            "• 💪 <b>Цель</b> — 2–3 предложения.\n"
            "• 📌 <b>Общие рекомендации</b> — 3–7 пунктов.\n"
            "• 🧱 <b>Структура тренировки</b> — по разделам: разминка, основная часть, заминка.\n"
            "  В основной части каждая строка: '• Упражнение — подходы × повторы (комментарий)'.\n"
            "• 🔁 <b>Прогрессия</b> — как увеличивать нагрузку.\n"
            "• ⚠️ <b>Важно</b> — 2–4 пункта по безопасности и самочувствию.\n"
            "Избегай markdown-таблиц, используй только списки."
        )
    elif intent == "daily_plan":
        extras.append(
            "Пользователь просит структурированный план/распорядок дня. "
            "Оформи ответ блоками:\n"
            "• 💡 <b>Кратко</b> — 2–3 предложения о цели дня.\n"
            "• 🌅 <b>Утро</b> — список дел по порядку.\n"
            "• 🌇 <b>День</b> — список ключевых блоков работы/учёбы/отдыха.\n"
            "• 🌙 <b>Вечер</b> — завершение дня и восстановление.\n"
            "• 📌 <b>Акценты</b> — 3–5 главных правил, которые важно не нарушать.\n"
            "Каждый пункт делай коротким, одна мысль — одна строка."
        )
    elif intent == "checklist":
        extras.append(
            "Пользователь хочет чёткий чек-лист действий. "
            "Сделай один блок <b>Чек-лист</b>, а внутри — пункты формата:\n"
            "• '☐ Сформулировать цель.'\n"
            "• '☐ Собрать документы.'\n"
            "• '☐ Проверить результат.'\n"
            "Каждый шаг — отдельная строка, максимум 10–15 шагов. "
            "Формулируй в повелительном наклонении (что нужно сделать)."
        )

    if extras:
        base += "\n\nДополнительные настройки формата и структуры ответа:\n- " + "\n- ".join(extras)

    return base


async def _call_llm(
    messages: List[dict],
    model_name: Optional[str] = None,
    *,
    postprocess: bool = True,
) -> str:
    """
    Общий helper для вызова LLM.
    postprocess=False используется там, где нам не нужны HTML-правки (например, при summary истории).
    """
    if _client is None:
        raise RuntimeError("Groq client is not configured (no API key).")

    if model_name is None:
        model_name = settings.groq_chat_model

    loop = asyncio.get_running_loop()

    def _do_request() -> str:
        completion = _client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.7,
            top_p=1,
            max_completion_tokens=2048,
            reasoning_effort="medium",
        )
        text = completion.choices[0].message.content or ""
        return text

    raw = await loop.run_in_executor(None, _do_request)
    if postprocess:
        return _postprocess_reply(raw)
    return raw


def _trim_history(state: ConversationState, max_turns: int = 12) -> None:
    """
    Обрезает историю, оставляя не более max_turns последних пар сообщений user+assistant.
    """
    if len(state.messages) > max_turns * 2:
        state.messages = state.messages[-max_turns * 2 :]


async def _maybe_summarize_history(user_id: int, state: ConversationState) -> None:
    """
    Если история стала слишком длинной — сжимаем её в краткий конспект,
    чтобы не терять контекст и не раздувать промпт.
    """
    max_turns_before_summary = 20
    if len(state.messages) <= max_turns_before_summary * 2:
        return

    history_json = json.dumps(state.messages, ensure_ascii=False)
    system_prompt = (
        "Ты — ассистент, который кратко конспектирует историю диалога между пользователем и ИИ. "
        "На вход ты получаешь JSON-список сообщений с полями role и content. "
        "Нужно сделать краткое структурированное резюме на русском языке: "
        "основные темы, важные решения, ключевые детали. Пиши с подзаголовками и списками."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": history_json},
    ]

    try:
        summary = await _call_llm(
            messages,
            model_name=settings.groq_chat_model_light,
            postprocess=False,
        )
    except Exception:
        logger.exception("Error while summarizing history")
        return

    state.messages = [
        {
            "role": "assistant",
            "content": "Краткое резюме предыдущего диалога:\n\n" + summary.strip(),
        }
    ]
    state.last_question = None
    state.last_answer = None
    _save_state(user_id, state)


async def ask_ai(user_id: int, text: str, user_name: Optional[str] = None) -> str:
    """
    Основная точка входа для обычных сообщений пользователя.
    """
    ok, retry_after, msg = check_rate_limit(user_id)
    if not ok:
        raise RateLimitError(retry_after=retry_after, message=msg)

    state = get_state(user_id)

    # При необходимости сжимаем историю
    await _maybe_summarize_history(user_id, state)

    intent = _classify_intent(text, state.mode_key)
    system_prompt = _personalize_system_prompt(state, user_name, intent=intent)

    messages: List[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(state.messages)
    messages.append({"role": "user", "content": text})

    try:
        reply = await _call_llm(
            messages,
            model_name=settings.groq_chat_model,
            postprocess=True,
        )
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

    await _maybe_summarize_history(user_id, state)

    system_prompt = _personalize_system_prompt(state, user_name, intent=None)
    messages: List[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(state.messages)
    continuation_request = (
        "Пожалуйста, продолжи свой предыдущий ответ, не повторяя уже написанное. "
        "Начни с того места, где мысль оборвалась."
    )
    messages.append({"role": "user", "content": continuation_request})

    try:
        reply = await _call_llm(
            messages,
            model_name=settings.groq_chat_model,
            postprocess=True,
        )
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
        - 'case'     -> структурированный клинический случай
    """
    state = get_state(user_id)
    if not state.last_answer:
        raise ValueError(
            "Пока нечего обрабатывать — сначала задай вопрос и получи ответ."
        )

    ok, retry_after, msg = check_rate_limit(user_id)
    if not ok:
        raise RateLimitError(retry_after=retry_after, message=msg)

    await _maybe_summarize_history(user_id, state)

    system_prompt = _personalize_system_prompt(state, user_name, intent=None)
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
    elif kind == "case":
        instr = (
            "Сделай структурированное описание клинического случая на основе моего предыдущего ответа. "
            "Структура: Жалобы; Анамнез; Объективные данные (если есть); "
            "Результаты обследований; Возможные диагностические гипотезы; "
            "Рекомендации по дальнейшему обследованию. Пиши чётко и по делу."
        )
    else:
        raise ValueError(f"Unknown transform kind: {kind}")

    user_msg = f"Вот предыдущий ответ ассистента:\n\n{state.last_answer}\n\n{instr}"
    messages.append({"role": "user", "content": user_msg})

    try:
        reply = await _call_llm(
            messages,
            model_name=settings.groq_chat_model_light,
            postprocess=True,
        )
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
