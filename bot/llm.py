import logging
from typing import Dict

import httpx

from bot.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    ASSISTANT_MODES,
    DEFAULT_MODE_KEY,
)


async def ask_llm(mode_key: str, user_prompt: str) -> str:
    """
    Единая точка общения с LLM (DeepSeek).
    mode_key — ключ режима ассистента, чтобы подставить правильный system_prompt.
    """
    mode_cfg: Dict[str, str] = ASSISTANT_MODES.get(
        mode_key,
        ASSISTANT_MODES[DEFAULT_MODE_KEY],
    )
    system_prompt = mode_cfg["system_prompt"]

    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "top_p": 0.9,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    try:
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        logging.error("Unexpected LLM response format: %s | data=%s", e, data)
        return "Произошла ошибка при обработке ответа модели. Попробуй ещё раз."
