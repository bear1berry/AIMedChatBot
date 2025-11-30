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
    Особенность Groq: для некоторых vision-моделей (llama-3.2-vision)
    нельзя одновременно использовать system-сообщение и картинку.
    Поэтому системный промпт зашиваем в текст user-сообщения.
    """
    if not settings.groq_api_key:
        return "❗️ GROQ_API_KEY не задан. Укажи ключ и перезапусти сервис."

    system_prompt = MODES["vision"]["system_prompt"]

    b64 = base64.b64encode(image_bytes).decode("ascii")

    url = settings.groq_base_url.rstrip("/") + "/chat/completions"

    # Один user-месседж с текстом + картинкой
    user_text = (
        f"{system_prompt}\n\n"
        "Теперь проанализируй это изображение с медицинской точки зрения. "
        "Сначала опиши, что видно, затем предположи возможные диагнозы, "
        "и обязательно подчеркни, что это не заменяет очный осмотр."
    )

    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": user_text,
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

    logger.info(
        "Sending vision request to Groq (user=%s, model=%s)",
        user_id,
        settings.groq_vision_model,
    )

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

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = e.response.text if e.response is not None else ""
            logger.error(
                "Vision HTTP error from Groq: %s\nStatus: %s\nBody: %s",
                e,
                e.response.status_code if e.response else "unknown",
                body,
            )
            return (
                "❗️ Не удалось проанализировать изображение (ошибка Groq).\n"
                f"Код: {e.response.status_code if e.response else '???'}\n"
                f"Детали: {body}"
            )

        try:
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.exception("Unexpected Groq vision response format: %s; body=%s", e, resp.text)
            return "❗️ Не удалось разобрать ответ от сервиса распознавания изображения."

    except Exception as e:
        logger.exception("Vision request failed: %s", e)
        return f"❗️ Не удалось проанализировать изображение: {e}"

    return reply
