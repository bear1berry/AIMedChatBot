# bot/vision.py
from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List

import httpx

from .config import settings
from .modes import MODES

logger = logging.getLogger(__name__)


async def analyze_image(image_bytes: bytes, user_id: int | None = None) -> str:
    """
    Анализ изображения с помощью vision-модели Groq.
    Если что-то пойдёт не так — вернём человекочитаемую ошибку.
    """
    if not settings.groq_api_key:
        return "❗️ GROQ_API_KEY не задан. Укажи ключ и перезапусти сервис."

    system_prompt = MODES["vision"]["system_prompt"]

    b64 = base64.b64encode(image_bytes).decode("ascii")

    url = settings.groq_base_url.rstrip("/") + "/chat/completions"

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Проанализируй это изображение с медицинской точки зрения. "
                            "Опиши, что видно, и предположи возможные диагнозы, если это уместно.",
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}",
                    },
                },
            ],
        },
    ]

    logger.info("Sending vision request to Groq (user=%s)", user_id)

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.groq_vision_model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": 800,
                },
            )
        resp.raise_for_status()
        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.exception("Vision request failed: %s", e)
        return f"❗️ Не удалось проанализировать изображение: {e}"

    return reply
