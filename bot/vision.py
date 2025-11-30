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
    Анализ изображения через vision-модель Groq.
    Для некоторых моделей запрещено отдельное system-сообщение,
    поэтому инструкции зашиваем в текст user-сообщения.
    """
    if not settings.groq_api_key:
        return "❗️ Не настроен GROQ_API_KEY, анализ изображений недоступен."

    system_prompt = MODES["vision"]["system_template"]

    b64 = base64.b64encode(image_bytes).decode("ascii")

    user_text = (
        f"{system_prompt}\n\n"
        "1) Сначала кратко опиши, что видно на изображении.\n"
        "2) Затем предположи несколько вариантов, что это может означать с медицинской точки зрения.\n"
        "3) Акцентируй внимание на признаках, требующих очного осмотра врача.\n"
        "4) Заверши чётким дисклеймером о том, что по фото диагноз не ставится."
    )

    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ],
        }
    ]

    url = settings.groq_base_url + "/chat/completions"

    logger.info(
        "Sending vision request to Groq model=%s user=%s",
        settings.groq_vision_model,
        user_id,
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
                    "temperature": 0.25,
                    "max_tokens": 800,
                },
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = e.response.text if e.response is not None else ""
            logger.error(
                "Vision HTTP error from Groq: %s status=%s body=%s",
                e,
                e.response.status_code if e.response else "unknown",
                body,
            )
            return (
                "❗️ Не удалось проанализировать изображение.\n"
                f"Код ошибки: {e.response.status_code if e.response else '??'}\n"
                f"Ответ сервера: {body[:800]}"
            )

        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.exception("Vision error: %s", e)
        return f"❗️ Ошибка при анализе изображения: {e}"

    return reply
