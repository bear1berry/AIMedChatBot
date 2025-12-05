import json
import logging
from typing import Dict, List, AsyncGenerator, Optional

import httpx

from bot.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    ASSISTANT_MODES,
    DEFAULT_MODE_KEY,
)

log = logging.getLogger(__name__)


def _build_messages(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    """
    Собираем список сообщений в формате OpenAI/DeepSeek.
    history — список словарей вида {"role": "...", "content": "..."}.
    """
    mode = ASSISTANT_MODES.get(mode_key) or ASSISTANT_MODES[DEFAULT_MODE_KEY]
    system_prompt = mode["system_prompt"]

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]

    if history:
        # Уже должны быть в формате {"role": ..., "content": ...}
        messages.extend(history)

    messages.append({"role": "user", "content": user_prompt})
    return messages


async def ask_llm_stream(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> AsyncGenerator[str, None]:
    """
    Стриминговый запрос к DeepSeek.
    Отдаём чанки текста по мере генерации.
    """
    messages = _build_messages(mode_key, user_prompt, history)

    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "stream": True,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                resp.raise_for_status()

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    # Протокол совместим с OpenAI: строки вида "data: {...}"
                    if line.startswith(":"):
                        # комментарий SSE
                        continue
                    if line.startswith("data:"):
                        data_str = line[len("data:"):].strip()
                    else:
                        data_str = line.strip()

                    if not data_str or data_str == "[DONE]":
                        break

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        log.warning("Failed to parse DeepSeek stream chunk: %r", data_str[:200])
                        continue

                    choices = data.get("choices")
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield content

    except httpx.HTTPStatusError as e:
        log.error("DeepSeek HTTP error: %s, response=%s", e, getattr(e, "response", None))
    except Exception as e:  # noqa: BLE001
        log.exception("DeepSeek streaming error: %s", e)


async def ask_llm_full(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """
    Нестриминговая обёртка: собирает все чанки в один ответ.
    """
    chunks: List[str] = []
    async for chunk in ask_llm_stream(mode_key, user_prompt, history):
        chunks.append(chunk)
    answer = "".join(chunks).strip()
    if not answer:
        return "Произошла ошибка при обработке ответа модели. Попробуй ещё раз."
    return answer
