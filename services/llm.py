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


def _build_messages(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> List[Dict[str, str]]:
    """
    Собираем messages для Chat Completions:
    [system] + history + [user]
    """
    mode_cfg: Dict[str, str] = ASSISTANT_MODES.get(
        mode_key,
        ASSISTANT_MODES[DEFAULT_MODE_KEY],
    )
    system_prompt = mode_cfg["system_prompt"]

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
    ]

    if history:
        # history — список словарей вида {"role": "user"/"assistant", "content": "..."}
        for msg in history:
            if msg.get("role") in {"user", "assistant"} and msg.get("content"):
                messages.append(
                    {"role": msg["role"], "content": msg["content"]},
                )

    messages.append({"role": "user", "content": user_prompt})
    return messages


async def ask_llm_stream(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> AsyncGenerator[str, None]:
    """
    Стриминговый вызов DeepSeek Chat Completions.
    Отдаёт куски текста по мере генерации (как async generator).
    """
    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": _build_messages(mode_key, user_prompt, history),
        "temperature": 0.7,
        "top_p": 0.9,
        "stream": True,
    }

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue

                # Стандартный формат: "data: {json}"
                if not line.startswith("data:"):
                    continue

                data_str = line[len("data:"):].strip()
                if not data_str or data_str == "[DONE]":
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    logging.warning("Cannot decode LLM stream chunk: %s", data_str)
                    continue

                try:
                    delta = data["choices"][0]["delta"]
                    content = delta.get("content")
                    if content:
                        yield content
                except (KeyError, IndexError) as e:
                    logging.warning("Unexpected stream chunk format: %s | %s", e, data)
                    continue


async def ask_llm(
    mode_key: str,
    user_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """
    Нестрмиминговая обёртка: собирает все чанки в один ответ.
    Можно использовать там, где стриминг не нужен.
    """
    chunks: List[str] = []
    async for chunk in ask_llm_stream(mode_key, user_prompt, history):
        chunks.append(chunk)
    answer = "".join(chunks).strip()
    if not answer:
        return "Произошла ошибка при обработке ответа модели. Попробуй ещё раз."
    return answer
