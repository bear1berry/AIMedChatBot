from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List

import httpx

from .modes import build_system_prompt, DEFAULT_MODE_KEY

logger = logging.getLogger(__name__)

# === AIMLAPI config ===

AIML_API_KEY = os.getenv("AIML_API_KEY", "").strip()
AIML_API_BASE = os.getenv("AIML_API_BASE", "https://api.aimlapi.com/v1").rstrip("/")

if not AIML_API_KEY:
    logger.warning("AIML_API_KEY is not set. LLM calls will fail.")

# Мягкий лимит на пользователя (как и раньше, в памяти процесса)
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "20"))
RATE_LIMIT_PER_DAY = int(os.getenv("RATE_LIMIT_PER_DAY", "500"))

# Модели по умолчанию — можно переопределить через переменные окружения
AIML_MODEL_PRIMARY = os.getenv("AIML_MODEL_PRIMARY", "openai/gpt-4.1")
AIML_MODEL_FAST = os.getenv("AIML_MODEL_FAST", "openai/gpt-4o-mini")
AIML_MODEL_GPT_OSS_120B = os.getenv("AIML_MODEL_GPT_OSS_120B", "openai/gpt-oss-120b")
AIML_MODEL_DEEPSEEK_REASONER = os.getenv("AIML_MODEL_DEEPSEEK_REASONER", "deepseek/deepseek-reasoner")
AIML_MODEL_DEEPSEEK_CHAT = os.getenv("AIML_MODEL_DEEPSEEK_CHAT", "deepseek/deepseek-chat")


@dataclass
class ConversationState:
    mode_key: str = DEFAULT_MODE_KEY
    messages: List[dict] = field(default_factory=list)
    # auto | gpt4 | mini | oss | deepseek_reasoner | deepseek_chat
    model_profile: str = "auto"


_conversations: Dict[int, ConversationState] = {}


@dataclass
class RateBucket:
    minute_ts: int = 0
    minute_count: int = 0
    day_ts: int = 0
    day_count: int = 0


_rate_buckets: Dict[int, RateBucket] = {}


class RateLimitError(Exception):
    """Raised when per-user rate limit is exceeded."""

    def __init__(self, scope: str):
        # scope: "minute" or "day"
        super().__init__(scope)
        self.scope = scope


def _check_rate_limit(user_id: int) -> None:
    now = int(time.time())
    minute = now // 60
    day = now // (24 * 60 * 60)

    bucket = _rate_buckets.get(user_id)
    if bucket is None:
        bucket = RateBucket(minute_ts=minute, day_ts=day)
        _rate_buckets[user_id] = bucket

    # reset windows if needed
    if bucket.minute_ts != minute:
        bucket.minute_ts = minute
        bucket.minute_count = 0

    if bucket.day_ts != day:
        bucket.day_ts = day
        bucket.day_count = 0

    if bucket.minute_count >= RATE_LIMIT_PER_MINUTE:
        raise RateLimitError("minute")

    if bucket.day_count >= RATE_LIMIT_PER_DAY:
        raise RateLimitError("day")

    bucket.minute_count += 1
    bucket.day_count += 1


# === State helpers ===


def get_state(user_id: int) -> ConversationState:
    state = _conversations.get(user_id)
    if state is None:
        state = ConversationState()
        _conversations[user_id] = state
    return state


def set_mode(user_id: int, mode_key: str) -> ConversationState:
    """
    Set chat mode for the user and clear history for a clean start.
    """
    state = get_state(user_id)
    state.mode_key = mode_key
    state.messages.clear()
    return state


def reset_state(user_id: int) -> None:
    """
    Clear only the history for the user but keep the current mode and model profile.
    """
    state = _conversations.get(user_id)
    if state:
        state.messages.clear()


_MODEL_PROFILE_LABELS = {
    "auto": "Авто (подбор моделей)",
    "gpt4": "GPT-4.1",
    "mini": "GPT-4o mini",
    "oss": "GPT-OSS 120B",
    "deepseek_reasoner": "DeepSeek Reasoner",
    "deepseek_chat": "DeepSeek Chat",
}


def set_model_profile(user_id: int, profile: str) -> ConversationState:
    """
    Установить профиль модели для пользователя.
    """
    if profile not in _MODEL_PROFILE_LABELS:
        raise ValueError(f"Unknown model profile: {profile}")
    state = get_state(user_id)
    state.model_profile = profile
    return state


def get_model_profile_label(profile: str) -> str:
    return _MODEL_PROFILE_LABELS.get(profile, "Авто (подбор моделей)")


# === LLM helpers ===

import re


def _postprocess_reply(text: str) -> str:
    """
    Лёгкая постобработка ответа модели:
    - подчистка лишних переносов
    - превращаем markdown-заголовки и **жирный** в HTML для Bot(parse_mode='HTML')
    - аккуратные маркеры списков
    """
    if not text:
        return "⚠️ Модель не вернула текст ответа."

    text = text.strip().replace("\r\n", "\n")

    # Сжимаем слишком большие расстояния
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Markdown **bold** -> <b>bold</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # Заголовки # / ## / ### -> <b>...</b>
    def _hdr(match: re.Match) -> str:
        content = match.group(1).strip()
        return f"<b>{content}</b>"

    text = re.sub(r"^#{1,3}\s+(.+)$", _hdr, text, flags=re.MULTILINE)

    # bullets "- " -> "• "
    text = re.sub(r"^- (.+)$", r"• \1", text, flags=re.MULTILINE)

    return text


def _model_human_name(model_id: str) -> str:
    if model_id == AIML_MODEL_PRIMARY:
        return "OpenAI GPT-4.1"
    if model_id == AIML_MODEL_FAST:
        return "GPT-4o mini"
    if model_id == AIML_MODEL_GPT_OSS_120B:
        return "GPT-OSS 120B"
    if model_id == AIML_MODEL_DEEPSEEK_REASONER:
        return "DeepSeek Reasoner"
    if model_id == AIML_MODEL_DEEPSEEK_CHAT:
        return "DeepSeek Chat"
    return model_id


def _model_emoji(model_id: str) -> str:
    if model_id == AIML_MODEL_PRIMARY:
        return "🧠"
    if model_id == AIML_MODEL_FAST:
        return "⚡️"
    if model_id == AIML_MODEL_GPT_OSS_120B:
        return "🧬"
    if model_id == AIML_MODEL_DEEPSEEK_REASONER:
        return "🧩"
    if model_id == AIML_MODEL_DEEPSEEK_CHAT:
        return "💬"
    return "🤖"


def _model_short_desc(model_id: str) -> str:
    if model_id == AIML_MODEL_PRIMARY:
        return "универсальная модель"
    if model_id == AIML_MODEL_FAST:
        return "быстрые ответы"
    if model_id == AIML_MODEL_GPT_OSS_120B:
        return "open-source гигант"
    if model_id == AIML_MODEL_DEEPSEEK_REASONER:
        return "глубокое рассуждение"
    if model_id == AIML_MODEL_DEEPSEEK_CHAT:
        return "диалоги и объяснения"
    return "модель"


def _is_reasoning_task(question: str) -> bool:
    q = question.lower()
    triggers = [
        "разбери", "обоснуй", "пошагово", "step by step", "разложи",
        "случай", "case", "диагноз", "дифференциальный", "план лечения",
        "анализируй", "analysis",
    ]
    return any(t in q for t in triggers) or len(q) > 1200


def _is_brainstorm_task(question: str) -> bool:
    q = question.lower()
    triggers = [
        "придумай", "варианты", "идеи", "brainstorm", "название", "title",
        "формулировка", "контент-план",
    ]
    return any(t in q for t in triggers)


def _is_code_task(question: str) -> bool:
    q = question.lower()
    triggers = [
        "python", "script", "скрипт", "код", "sql", "javascript", "js",
    ]
    return any(t in q for t in triggers)


def _select_models_for_query(question: str, state: ConversationState) -> list[str]:
    """
    Возвращает список id моделей, которые нужно дернуть для ответа.
    Если выбрана ручная модель — всегда одна.
    В режиме auto — 1–2 модели в зависимости от задачи.
    """
    profile = state.model_profile

    # Ручной выбор — всегда ровно одна модель
    if profile == "gpt4":
        return [AIML_MODEL_PRIMARY]
    if profile == "mini":
        return [AIML_MODEL_FAST]
    if profile == "oss":
        return [AIML_MODEL_GPT_OSS_120B]
    if profile == "deepseek_reasoner":
        return [AIML_MODEL_DEEPSEEK_REASONER]
    if profile == "deepseek_chat":
        return [AIML_MODEL_DEEPSEEK_CHAT]

    # Авто-подбор
    is_reasoning = _is_reasoning_task(question)
    is_brainstorm = _is_brainstorm_task(question)
    is_code = _is_code_task(question)

    # Сложные кейсы / код — 2 модели: GPT-4.1 + DeepSeek Reasoner
    if is_reasoning or is_code:
        return [AIML_MODEL_PRIMARY, AIML_MODEL_DEEPSEEK_REASONER]

    # Брейншторм / креатив — GPT-OSS 120B + GPT-4.1
    if is_brainstorm:
        return [AIML_MODEL_GPT_OSS_120B, AIML_MODEL_PRIMARY]

    # Обычный короткий вопрос — быстрая модель
    if len(question) < 400:
        return [AIML_MODEL_FAST]

    # Остальное — основная модель
    return [AIML_MODEL_PRIMARY]


async def _call_model(model: str, messages: list[dict]) -> str:
    """
    Вызов одной модели AIMLAPI.
    """
    if not AIML_API_KEY:
        raise RuntimeError("AIML_API_KEY is not configured")

    url = f"{AIML_API_BASE}/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "top_p": 1.0,
        "max_tokens": 2048,
    }

    headers = {
        "Authorization": f"Bearer {AIML_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
        try:
            data = resp.json()
        except Exception:
            logger.exception("Failed to parse AIMLAPI response: %s", resp.text[:500])
            raise

    if resp.status_code >= 400:
        err = data.get("error") if isinstance(data, dict) else data
        logger.error("AIMLAPI error (%s): %r", resp.status_code, err)
        raise RuntimeError(f"AIMLAPI error {resp.status_code}: {err}")

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        logger.exception("Unexpected AIMLAPI payload: %r", data)
        raise RuntimeError("Unexpected AIMLAPI response format")

    return _postprocess_reply(content or "")


# === Public API ===


async def ask_ai(user_id: int, text: str, user_name: str | None = None) -> str:
    """
    Main entry point: send user message to LLM and get assistant reply.
    Conversation history, mode and model profile are handled automatically.
    """
    _check_rate_limit(user_id)

    state = get_state(user_id)

    system_prompt = build_system_prompt(mode_key=state.mode_key, user_name=user_name)
    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(state.messages)
    messages.append({"role": "user", "content": text})

    models = _select_models_for_query(text, state)

    # Один профиль / модель
    if len(models) == 1:
        reply = await _call_model(models[0], messages)
    else:
        # Несколько моделей — запускаем параллельно и красиво объединяем ответы
        tasks = [_call_model(m, messages) for m in models]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        blocks: list[str] = []
        for model_id, result in zip(models, results):
            name = _model_human_name(model_id)
            emoji = _model_emoji(model_id)
            desc = _model_short_desc(model_id)

            if isinstance(result, Exception):
                logger.exception("Model %s failed", model_id, exc_info=result)
                block = (
                    f"{emoji} <b>{name}</b> ({desc}):\n"
                    "⚠️ Ошибка при обращении к модели. Попробуй ещё раз."
                )
            else:
                block = f"{emoji} <b>{name}</b> ({desc}):\n{result}"

            blocks.append(block)

        reply = "\n\n━━━━━━━━━━━━━━\n\n".join(blocks)

    # update state history
    state.messages.append({"role": "user", "content": text})
    state.messages.append({"role": "assistant", "content": reply})

    # truncate history (keep last N turns)
    max_turns = 12  # user+assistant pair = 2 messages
    if len(state.messages) > max_turns * 2:
        state.messages = state.messages[-max_turns * 2 :]

    return reply


async def healthcheck_llm() -> bool:
    """
    Lightweight ping to check model availability.
    """
    try:
        _ = await _call_model(AIML_MODEL_FAST, [{"role": "user", "content": "ping"}])
        return True
    except Exception:
        logger.exception("LLM healthcheck failed")
        return False
