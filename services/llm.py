from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator, Dict, Any, List

import httpx

from bot.config import DEEPSEEK_API_KEY, DEEPSEEK_API_URL, DEEPSEEK_MODEL, ASSISTANT_MODES

logger = logging.getLogger(__name__)


async def _call_deepseek(messages: List[Dict[str, str]]) -> str:
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(DEEPSEEK_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.exception("Unexpected DeepSeek response: %s", data)
            raise RuntimeError("Unexpected DeepSeek response") from e


def _estimate_tokens(*texts: str) -> int:
    # Небольшая грубая оценка токенов
    total_chars = sum(len(t or "") for t in texts)
    return max(1, total_chars // 4)


async def ask_llm_stream(
    mode_key: str,
    user_prompt: str,
    style_hint: str | None = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Возвращает async-генератор с частями текста.
    Каждый yield: {"delta": str, "full": str, "tokens": int}
    """
    mode = ASSISTANT_MODES.get(mode_key) or ASSISTANT_MODES["universal"]
    system_prompt = mode["system_prompt"]
    if style_hint:
        system_prompt += (
            "\n\nДополнительно учитывай стиль общения пользователя:\n"
            f"{style_hint.strip()}"
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    full_text = await _call_deepseek(messages)
    est_tokens = _estimate_tokens(user_prompt, full_text)

    # Плавное "живое" печатание — отдаём текст батчами
    words = full_text.split()
    chunks: List[str] = []
    current = ""
    for w in words:
        candidate = (current + " " + w) if current else w
        if len(candidate) >= 120:  # размер батча
            chunks.append(candidate)
            current = ""
        else:
            current = candidate
    if current:
        chunks.append(current)

    assembled = ""
    for chunk in chunks:
        assembled = (assembled + " " + chunk) if assembled else chunk
        yield {
            "delta": chunk,
            "full": assembled,
            "tokens": est_tokens,
        }
        await asyncio.sleep(0.05)

    if not chunks:
        # На всякий случай, если текст оказался пустым
        yield {"delta": "", "full": full_text, "tokens": est_tokens}
