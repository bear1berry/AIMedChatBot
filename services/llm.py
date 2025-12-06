from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncGenerator, Dict, Any, List

import httpx

from bot.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_API_URL,
    DEEPSEEK_MODEL,
    ASSISTANT_MODES,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# Модели DeepSeek: лёгкая и тяжёлая
# ------------------------------------------------------------
# По умолчанию обе = DEEPSEEK_MODEL, чтобы ничего не ломать.
# Можешь в .env задать:
#   DEEPSEEK_LIGHT_MODEL=deepseek-chat
#   DEEPSEEK_HEAVY_MODEL=deepseek-reasoner
DEEPSEEK_LIGHT_MODEL = os.getenv("DEEPSEEK_LIGHT_MODEL") or DEEPSEEK_MODEL
DEEPSEEK_HEAVY_MODEL = os.getenv("DEEPSEEK_HEAVY_MODEL") or DEEPSEEK_MODEL


# ------------------------------------------------------------
# Вызов DeepSeek
# ------------------------------------------------------------
async def _call_deepseek(
    messages: List[Dict[str, str]],
    model: str,
) -> str:
    """
    Низкоуровневый вызов DeepSeek Chat Completion (без стрима).
    """
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(DEEPSEEK_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        logger.exception("Unexpected DeepSeek response: %s", data)
        raise RuntimeError("Unexpected DeepSeek response")


def _estimate_tokens(*texts: str) -> int:
    """
    Небольшая грубая оценка токенов: 1 токен ~ 4 символа.
    Нужна для лимитов/метрик.
    """
    total_chars = sum(len(t or "") for t in texts)
    return max(1, total_chars // 4)


# ------------------------------------------------------------
# Интент и роутинг по моделям
# ------------------------------------------------------------
def _detect_intent_for_routing(text: str) -> str:
    """
    Очень лёгкий детектор типа интента по тексту для руутинга:
    - 'plan'       — запрос плана/шагов
    - 'coaching'   — наставничество/прокачка
    - 'reflection' — анализ/разбор
    - 'question'   — обычный вопрос
    - 'other'      — всё остальное
    """
    if not text:
        return "other"

    t = text.lower()

    plan_markers = [
        "сделай план",
        "по шагам",
        "шаг за шагом",
        "чек-лист",
        "чеклист",
        "roadmap",
        "дорожную карту",
    ]
    if any(m in t for m in plan_markers):
        return "plan"

    coaching_markers = [
        "мотивац",
        "дисциплин",
        "привычк",
        "распорядок",
        "режим дня",
        "как мне стать",
        "хочу прокачать",
        "как научиться",
        "как перестать",
        "как начать",
    ]
    if any(m in t for m in coaching_markers):
        return "coaching"

    reflection_markers = [
        "проанализируй",
        "анализ",
        "разбор",
        "разложи по полочкам",
        "объясни",
        "почему так",
        "что я делаю не так",
    ]
    if any(m in t for m in reflection_markers):
        return "reflection"

    question_markers = [
        "почему",
        "зачем",
        "как",
        "что",
        "когда",
        "где",
        "сколько",
        "можно ли",
        "?",
    ]
    if any(m in t for m in question_markers):
        return "question"

    return "other"


def _select_model_for_prompt(user_prompt: str, mode_key: str) -> str:
    """
    Умный выбор модели на основе:
    - длины запроса,
    - типа интента,
    - активного режима (медицина/бизнес/коуч и т.п.).
    """

    intent = _detect_intent_for_routing(user_prompt)
    length = len(user_prompt or "")
    mode = mode_key or "universal"

    # Медицина — всегда heavy (осторожность, качество)
    if mode == "medicine":
        selected = DEEPSEEK_HEAVY_MODEL
    # Очень длинные/сложные запросы — heavy
    elif length > 1200:
        selected = DEEPSEEK_HEAVY_MODEL
    # Планы / коучинг / глубокий разбор — лучше heavy
    elif intent in ("plan", "coaching", "reflection"):
        selected = DEEPSEEK_HEAVY_MODEL
    else:
        # small talk / короткие вопросы — лёгкая модель
        selected = DEEPSEEK_LIGHT_MODEL

    logger.debug(
        "LLM routing: mode=%s intent=%s length=%d → model=%s",
        mode,
        intent,
        length,
        selected,
    )
    return selected


# ------------------------------------------------------------
# Публичный интерфейс: ask_llm_stream
# ------------------------------------------------------------
async def ask_llm_stream(
    mode_key: str,
    user_prompt: str,
    style_hint: str | None = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Возвращает async-генератор с частями текста.

    Каждый yield:
        {
            "delta": str,     # очередной смысловой кусок
            "full": str,      # полный текст на данный момент
            "tokens": int,    # грубая оценка токенов
            "model": str,     # фактическая модель DeepSeek (light/heavy)
        }
    """

    # 1. Берём режим и системный промпт
    mode_cfg = ASSISTANT_MODES.get(mode_key) or ASSISTANT_MODES.get("universal") or next(
        iter(ASSISTANT_MODES.values())
    )
    system_prompt = mode_cfg["system_prompt"]

    if style_hint:
        system_prompt += (
            "\n\nДополнительно учитывай стиль общения пользователя:\n"
            f"{style_hint.strip()}"
        )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # 2. Умный выбор модели (light/heavy)
    model_name = _select_model_for_prompt(user_prompt, mode_key)

    # 3. Запрос к DeepSeek
    full_text = await _call_deepseek(messages, model=model_name)
    est_tokens = _estimate_tokens(user_prompt, full_text)

    # 4. Плавное "живое" печатание — отдаём текст батчами по смыслу
    words = (full_text or "").split()
    chunks: List[str] = []
    current = ""

    for w in words:
        candidate = (current + " " + w) if current else w
        # Примерный размер батча: ~120 символов
        if len(candidate) >= 120:
            chunks.append(candidate)
            current = ""
        else:
            current = candidate

    if current:
        chunks.append(current)

    assembled = ""
    if chunks:
        for chunk in chunks:
            assembled = (assembled + " " + chunk) if assembled else chunk
            yield {
                "delta": chunk,
                "full": assembled,
                "tokens": est_tokens,
                "model": model_name,
            }
            # лёгкая пауза, чтобы печать ощущалась «живой», но быстрой
            await asyncio.sleep(0.05)
    else:
        # На всякий случай, если текст оказался пустым
        yield {
            "delta": "",
            "full": full_text or "",
            "tokens": est_tokens,
            "model": model_name,
        }
