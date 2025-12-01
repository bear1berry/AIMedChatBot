from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def analyze_image(image_bytes: bytes, user_id: int | None = None) -> str:
    """
    Заглушка: сейчас GPT-OSS 120B через Groq используется только для текста.
    """
    logger.info("analyze_image called, but vision is not implemented")
    return (
        "Пока эта версия бота умеет работать только с текстом 💬.\n"
        "Но я с радостью помогу ответить на любые текстовые вопросы!"
    )
