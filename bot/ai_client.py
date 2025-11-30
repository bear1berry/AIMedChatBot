import logging
from typing import List, Dict

import httpx

from .config import settings
from .modes import MODES
from .memory import get_history, log_message

logger = logging.getLogger(__name__)

GROQ_API_URL = url = "https://api.groq.com/v1/chat/completions"


async def ask_ai(user_id: int, mode: str, user_message: str) -> str:
    """
    Запрос к текстовой модели Groq с учётом истории диалога.
    """
    mode_obj = MODES.get(mode, MODES["default"])

    history = get_history(user_id, limit=10)

    messages: List[Dict] = [
        {"role": "system", "content": mode_obj.system_prompt}
    ]

    for item in history:
        messages.append({"role": item["role"], "content": item["text"]})

    messages.append({"role": "user", "content": user_message})

    logger.info("Sending request to Groq for user %s in mode %s", user_id, mode)

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            json={
                "model": settings.model_name,
                "messages": messages,
                "max_tokens": 800,
                "temperature": 0.7,
            },
        )

    resp.raise_for_status()
    data = resp.json()
    reply = data["choices"][0]["message"]["content"].strip()

    log_message(user_id, "assistant", reply, mode)

    logger.info(
        "Groq response for user %s (mode %s): %s",
        user_id,
        mode,
        reply[:200].replace("\n", " "),
    )
    return reply

