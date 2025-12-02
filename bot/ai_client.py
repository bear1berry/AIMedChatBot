from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional, Tuple

from groq import AsyncGroq

from .config import settings
from .limits import check_rate_limit
from .modes import DEFAULT_MODE_KEY, build_system_prompt, get_mode_label

logger = logging.getLogger(__name__)

# --- Типы настроек диалога ---

AnswerStyle = Literal["default", "short", "detailed", "checklist"]
ToneStyle = Literal["default", "story", "strict"]
Audience = Literal["auto", "patient", "doctor"]
ModelProfile = Literal[
    "auto",
    "gpt4",
    "mini",
    "oss",
    "deepseek_reasoner",
    "deepseek_chat",
]


_MODEL_PROFILE_LABELS: Dict[str, str] = {
    "auto": "🤖 Авто (подбор модели)",
    "gpt4": "🧠 GPT-4.1 (профиль)",
    "mini": "⚡️ GPT-4o mini (профиль)",
    "oss": "🧬 GPT-OSS 120B",
    "deepseek_reasoner": "🧩 DeepSeek Reasoner (профиль)",
    "deepseek_chat": "💬 DeepSeek Chat (профиль)",
}


class RateLimitError(Exception):
    """Исключение, выбрасываемое при превышении лимитов запросов."""

    def __init__(
        self,
        scope: str,
        retry_after: Optional[int],
        message: Optional[str] = None,
    ) -> None:
        super().__init__(message or "Rate limit exceeded")
        self.scope = scope          # "minute" или "day"
        self.retry_after = retry_after
        self.message = message or ""


@dataclass
class ConversationState:
    mode_key: str = DEFAULT_MODE_KEY
    messages: List[Dict[str, str]] = field(default_factory=list)
    answer_style: AnswerStyle = "default"
    tone: ToneStyle = "default"
    audience: Audience = "auto"
    model_profile: ModelProfile = "auto"


_STATES: Dict[int, ConversationState] = {}

_client = AsyncGroq(api_key=settings.groq_api_key)


def get_state(user_id: int) -> ConversationState:
    state = _STATES.get(user_id)
    if state is None:
        state = ConversationState()
        _STATES[user_id] = state
    return state


def reset_state(user_id: int) -> None:
    """Полный сброс состояния диалога пользователя в памяти."""
    _STATES[user_id] = ConversationState()


def set_mode(user_id: int, mode_key: str) -> ConversationState:
    """Переключение режима ассистента для пользователя."""
    state = get_state(user_id)
    state.mode_key = mode_key or DEFAULT_MODE_KEY
    state.messages.clear()
    logger.info("User %s switched mode to %s", user_id, get_mode_label(state.mode_key))
    return state


def set_model_profile(user_id: int, profile: str) -> ConversationState:
    """Сохраняем выбранный профиль модели (для UI и возможных будущих настроек)."""
    if profile not in _MODEL_PROFILE_LABELS:
        raise ValueError(f"Unknown model profile: {profile}")

    state = get_state(user_id)
    state.model_profile = profile  # type: ignore[assignment]
    logger.info("User %s switched model profile to %s", user_id, profile)
    return state


def get_model_profile_label(profile: Optional[str]) -> str:
    if not profile:
        return _MODEL_PROFILE_LABELS["auto"]
    return _MODEL_PROFILE_LABELS.get(profile, _MODEL_PROFILE_LABELS["auto"])


async def healthcheck_llm() -> bool:
    """Простой ping-запрос к модели Groq, чтобы проверить доступность API."""
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


# --- Хелперы анализа текста ---


def _classify_task_type(text: str) -> str:
    t = text.lower()

    post_triggers = [
        "пост для",
        "пост в",
        "для канала",
        "описание канала",
        "описание профиля",
        "контент-план",
        "контент план",
        "заголовок поста",
        "текст для поста",
    ]
    outline_triggers = [
        "конспект",
        "шпаргалк",
        "структуру",
        "структура лекции",
        "план лекции",
        "план доклада",
        "outline",
    ]
    plan_triggers = [
        "пошаговый план",
        "что делать",
        "todo",
        "to-do",
        "список задач",
        "дорожную карту",
    ]
    code_triggers = [
        "код",
        "script",
        "скрипт",
        "python",
        "sql",
        "javascript",
        "ошибка",
        "traceback",
        "stack trace",
    ]

    if any(w in t for w in code_triggers):
        return "code"
    if any(w in t for w in post_triggers):
        return "post"
    if any(w in t for w in outline_triggers):
        return "outline"
    if any(w in t for w in plan_triggers):
        return "plan"
    return "chat"


def _detect_audience_from_text(text: str) -> Audience:
    t = text.lower()
    patient_triggers = [
        "для пациента",
        "понятным языком",
        "простым языком",
        "для обычных людей",
    ]
    doctor_triggers = [
        "для врача",
        "для студентов-медиков",
        "для ординаторов",
        "для медиков",
        "лекция для",
    ]

    if any(w in t for w in patient_triggers):
        return "patient"
    if any(w in t for w in doctor_triggers):
        return "doctor"
    return "auto"


_MEDICAL_KEYWORDS = [
    "диагноз",
    "лечение",
    "лечить",
    "симптом",
    "жалоб",
    "пациент",
    "температур",
    "кашель",
    "боль",
    "высыпан",
    "анализ",
    "кровь",
    "пневмони",
    "инфекц",
    "эпид",
    "вакцин",
    "прививк",
]


def _is_medical_context(text: str, mode_key: str) -> bool:
    if mode_key == "ai_medicine_assistant":
        return True
    t = text.lower()
    return any(w in t for w in _MEDICAL_KEYWORDS)


_RED_FLAG_KEYWORDS = [
    "резкая боль в груди",
    "сильная боль в груди",
    "одышк",
    "не может дышать",
    "удушье",
    "потеря сознания",
    "упал в обморок",
    "судорог",
    "онемение руки",
    "перекосило лицо",
    "нарушение речи",
    "кровавая рвота",
    "рвота с кровью",
    "черный стул",
    "дёгтеобразный стул",
    "температура 40",
    "температура 39",
    "очень сильная боль в животе",
]


def _has_medical_red_flags(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in _RED_FLAG_KEYWORDS)


def _build_dynamic_instructions(state: ConversationState, user_text: str) -> str:
    parts: List[str] = []

    # Тип задачи
    task_type = _classify_task_type(user_text)
    if task_type == "post":
        parts.append(
            "- Формат ответа: текст для поста в Telegram с коротким цепляющим вступлением и 2–4 абзацами. Если уместно, добавь короткий список."
        )
    elif task_type == "outline":
        parts.append(
            "- Формат ответа: структурированный конспект с заголовками и списками."
        )
    elif task_type == "plan":
        parts.append(
            "- Формат ответа: пошаговый план действий (чек-лист), каждый шаг с новой строки."
        )
    elif task_type == "code":
        parts.append(
            "- Формат ответа: сначала краткое объяснение, затем пример кода. Избегай лишнего использования Markdown-форматирования внутри кода."
        )
    else:
        parts.append("- Формат ответа: обычное объяснение, структурно и по делу.")

    # Стиль длины ответа
    if state.answer_style == "short":
        parts.append(
            "- Длина ответа: максимально кратко, 3–7 ключевых пунктов или предложений, только суть."
        )
    elif state.answer_style == "detailed":
        parts.append(
            "- Длина ответа: подробно, но без воды. Разделяй текст на логические блоки с заголовками и списками."
        )
    elif state.answer_style == "checklist":
        parts.append(
            "- Формат ответа: чек-лист. Каждый пункт с новой строки, начинай с символа «•»."
        )

    # Тон
    if state.tone == "story":
        parts.append(
            "- Тон: лёгкий сторителлинг. Сначала короткий контекст или мини-история, затем выводы и рекомендации."
        )
    elif state.tone == "strict":
        parts.append(
            "- Тон: сухо, структурно и по делу, без лишних эмоций и украшательств."
        )

    # Аудитория
    effective_audience = state.audience
    if effective_audience == "auto":
        detected = _detect_audience_from_text(user_text)
        if detected != "auto":
            effective_audience = detected

    if effective_audience == "patient":
        parts.append(
            "- Аудитория: пациент или человек без медобразования. Объясняй максимально простым и понятным языком, избегай тяжёлых терминов или сразу расшифровывай их."
        )
    elif effective_audience == "doctor":
        parts.append(
            "- Аудитория: врач или студент-медик. Можно использовать медицинскую терминологию, ориентируйся на профессиональный уровень."
        )

    if not parts:
        return ""

    return "Дополнительные настройки ответа:\n" + "\n".join(parts)


# --- Постобработка текста под HTML parse_mode ---


_ALLOWED_HTML_TAGS = {"b", "strong", "i", "em", "u", "code", "a"}


def _strip_unsupported_html(text: str) -> str:
    """Удаляем все HTML-теги, кроме безопасных для Telegram."""

    def _repl(match: re.Match) -> str:
        tag = match.group("tag").lower()
        if tag in _ALLOWED_HTML_TAGS:
            return match.group(0)
        return ""

    return re.sub(
        r"</?(?P<tag>[a-zA-Z0-9]+)(?:\s[^>]*?)?>",
        _repl,
        text,
    )


def _convert_simple_markdown_to_html(text: str) -> str:
    """Мини-конвертер самых частых Markdown-паттернов в HTML."""
    # **bold** или *bold* -> <b>bold</b>
    text = re.sub(
        r"\*\*(?P<inner>[^*]+)\*\*", r"<b>\g<inner></b>", text
    )
    text = re.sub(
        r"\*(?P<inner>[^*]+)\*", r"<b>\g<inner></b>", text
    )

    # _italic_ -> <i>italic</i>
    text = re.sub(
        r"_(?P<inner>[^_]+)_", r"<i>\g<inner></i>", text
    )
    return text


def _postprocess_reply(text: str) -> str:
    """Приводим ответ к аккуратной HTML-разметке для Telegram."""
    if not text:
        return "Извини, сейчас не получилось сформировать ответ."

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    # Простейшие теги абзацев -> перевод строки
    text = re.sub(
        r"</?(?:p|br|div|span)\s*/?>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )

    # Нумерованные списки "1. ..." / "1) ..." -> "• ..."
    text = re.sub(
        r"^\s*\d+[.)]\s+",
        "• ",
        text,
        flags=re.MULTILINE,
    )

    # "- пункт" в начале строки -> "• пункт"
    text = re.sub(
        r"^\s*-\s+",
        "• ",
        text,
        flags=re.MULTILINE,
    )

    # Заголовки вида "# Заголовок" -> <b>Заголовок</b>
    def heading_repl(match: re.Match) -> str:
        title = match.group("title").strip()
        if not title:
            return ""
        return f"<b>{title}</b>\n"

    text = re.sub(
        r"^(?P<hashes>#{1,3})\s+(?P<title>.+)$",
        heading_repl,
        text,
        flags=re.MULTILINE,
    )

    # Конвертируем простейший Markdown в HTML
    text = _convert_simple_markdown_to_html(text)

    # Убираем лишние и опасные HTML-теги
    text = _strip_unsupported_html(text)

    # Сжимаем пачки пустых строк
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _select_model_and_params(state: ConversationState) -> Tuple[str, float]:
    """Подбор имени модели и температуры по профилю.

    Сейчас всё завязано на одном значении settings.model_name, но профиль
    сохраняем на будущее и слегка корректируем температуру.
    """
    profile = state.model_profile

    # Базовое значение
    model_name = settings.model_name
    temperature = 0.35

    if profile == "mini":
        temperature = 0.4
    elif profile == "deepseek_reasoner":
        temperature = 0.25
    elif profile == "deepseek_chat":
        temperature = 0.5
    # gpt4 / oss / auto остаются с дефолтной температурой

    return model_name, temperature


# --- Основной вызов модели ---


async def ask_ai(user_id: int, text: str, user_name: Optional[str] = None) -> str:
    """Основной вызов модели Groq с учётом режима, истории и настроек стиля."""
    state = get_state(user_id)
    mode_key = state.mode_key or DEFAULT_MODE_KEY

    # Проверяем rate-limit
    ok, retry_after, scope, msg = check_rate_limit(user_id)
    if not ok:
        raise RateLimitError(scope or "minute", retry_after, msg)

    base_system = build_system_prompt(mode_key, user_name=user_name)
    dynamic = _build_dynamic_instructions(state, text)
    system_prompt = base_system
    if dynamic:
        system_prompt = base_system + "\n\n" + dynamic

    # История (до ~40 сообщений)
    history = state.messages[-40:]

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": text},
    ]

    model_name, temperature = _select_model_and_params(state)

    try:
        completion = await _client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=temperature,
            max_completion_tokens=1800,
        )
    except Exception as e:
        logger.exception("Groq API error: %s", e)
        raise

    reply = completion.choices[0].message.content if completion.choices else ""
    reply = reply or "Извини, я не смог сформировать осмысленный ответ."

    reply = _postprocess_reply(reply)

    # Медицинские флаги / дисклеймеры
    is_med = _is_medical_context(text, mode_key)
    has_flags = _has_medical_red_flags(text)

    if is_med:
        disclaimer = (
            "⚠️ Этот ответ носит информационный характер и не является заменой очной "
            "консультации врача. Для постановки диагноза и назначения лечения обязательно "
            "обратитесь к специалисту."
        )
        if disclaimer not in reply:
            reply = f"{reply}\n\n{disclaimer}"

    if has_flags:
        emergency = (
            "🚨 В описанной ситуации могут присутствовать симптомы, требующие срочной "
            "оценки врачом.\n"
            "Если есть выраженная боль, затруднённое дыхание, нарушение сознания, судороги, "
            "признаки инсульта или другие острые симптомы — немедленно вызовите скорую помощь "
            "или обратитесь в ближайший приёмный покой."
        )
        reply = f"{emergency}\n\n{reply}"

    # Заголовок с режимом
    mode_label = get_mode_label(mode_key)
    reply = f"<b>{mode_label}</b>\n\n{reply}"

    # Сохраняем историю
    state.messages.append({"role": "user", "content": text})
    state.messages.append({"role": "assistant", "content": reply})

    return reply


# --- Дополнительные функции для /summary, /todo, /md, /status ---


async def summarize_dialog(user_id: int) -> str:
    state = get_state(user_id)
    if not state.messages:
        return "Диалог пока пуст — нечего резюмировать."

    history = state.messages[-40:]
    messages = [
        {
            "role": "system",
            "content": (
                "Ты делаешь краткое резюме диалога между пользователем и ассистентом.\n"
                "Сформируй 3–7 ключевых пункта: о чём говорили, какие решения и идеи появились."
            ),
        },
        *history,
    ]

    completion = await _client.chat.completions.create(
        model=settings.model_name,
        messages=messages,
        temperature=0.2,
        max_completion_tokens=600,
    )
    reply = completion.choices[0].message.content if completion.choices else ""
    return _postprocess_reply(reply or "Не удалось построить резюме диалога.")


async def extract_todos(user_id: int) -> str:
    state = get_state(user_id)
    if not state.messages:
        return "Диалог пока пуст — задач не видно."

    history = state.messages[-40:]
    messages = [
        {
            "role": "system",
            "content": (
                "Извлеки из диалога список задач и конкретных действий для пользователя.\n"
                "Формат ответа: чек-лист с пунктами, каждый пункт с новой строки, начинать с «•».\n"
                "Если явных задач нет — покажи возможные шаги, которые логично вытекают из обсуждения."
            ),
        },
        *history,
    ]

    completion = await _client.chat.completions.create(
        model=settings.model_name,
        messages=messages,
        temperature=0.25,
        max_completion_tokens=600,
    )
    reply = completion.choices[0].message.content if completion.choices else ""
    return _postprocess_reply(reply or "Я не вижу явных задач в этом диалоге.")


def export_markdown(user_id: int) -> str:
    state = get_state(user_id)
    if not state.messages:
        return "Диалог пока пуст."

    history = state.messages[-80:]

    lines: List[str] = []
    for msg in history:
        role = msg.get("role", "")
        content = msg.get("content", "").strip()
        if not content:
            continue
        if role == "user":
            lines.append(f"*User:*\n{content}")
        elif role == "assistant":
            lines.append(f"*Assistant:*\n{content}")
        else:
            lines.append(f"*{role}:*\n{content}")

    return "\n\n---\n\n".join(lines)


def get_user_settings(user_id: int) -> Dict[str, str]:
    state = get_state(user_id)
    return {
        "mode_key": state.mode_key,
        "mode_label": get_mode_label(state.mode_key),
        "answer_style": state.answer_style,
        "tone": state.tone,
        "audience": state.audience,
        "model_profile": state.model_profile,
        "messages_count": str(len(state.messages)),
        "model_name": settings.model_name,
    }
