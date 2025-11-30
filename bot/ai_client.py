# bot/ai_client.py
from __future__ import annotations

import logging
from typing import List, Dict, Any

import httpx

from .config import settings
from .modes import build_system_prompt
from .memory import get_history, save_message

logger = logging.getLogger(__name__)


async def ask_ai(
    user_id: int,
    mode: str,
    user_message: str,
) -> str:
    """
    Запрос к Groq (chat.completions) с учётом режима и истории диалога.
    """
    if not settings.groq_api_key:
        return "❗️ GROQ_API_KEY не задан в переменных окружения. Укажи ключ и перезапусти сервис."

    system_prompt = build_system_prompt(user_id, mode)

    history = get_history(user_id, limit=12)

    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]

    for item in history:
        # в базе храним только user/assistant
        role = "assistant" if item["role"] == "assistant" else "user"
        messages.append({"role": role, "content": item["content"]})

    messages.append({"role": "user", "content": user_message})

    url = settings.groq_base_url.rstrip("/") + "/chat/completions"

    logger.info(
        "Sending request to Groq for user %s in mode %s with model %s",
        user_id,
        mode,
        settings.groq_chat_model,
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.groq_chat_model,
                    "messages": messages,
                    "temperature": 0.4,
                    "max_tokens": 1024,
                },
            )

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = e.response.text if e.response is not None else ""
            logger.error(
                "HTTP error from Groq: %s\nStatus: %s\nBody: %s",
                e,
                e.response.status_code if e.response else "unknown",
                body,
            )
            return (
                f"❗️ Ошибка при запросе к Groq: {e.response.status_code if e.response else '???'}.\n"
                f"URL: {url}\n"
                f"Детали: {body}\n"
                "Проверь модель, ключ API и доступность сервиса."
            )

        # Нормальный ответ
        try:
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.exception("Unexpected Groq response format: %s; body=%s", e, resp.text)
            return (
                "❗️ Сервис Groq вернул неожиданный ответ. "
                "Попробуй ещё раз чуть позже."
            )

    except Exception as e:
        logger.exception("Error while requesting Groq: %s", e)
        return f"❗️ Внутренняя ошибка при запросе к Groq: {e}"

    # Логируем в БД и файл
    save_message(user_id, "user", user_message, mode)
    save_message(user_id, "assistant", reply, mode)

    if settings.query_log_file:
        try:
            with open(settings.query_log_file, "a", encoding="utf-8") as f:
                f.write(
                    f"USER_ID={user_id} MODE={mode}\n"
                    f"USER: {user_message}\n"
                    f"ASSISTANT: {reply[:800]}\n"
                    "----------------------------------------\n"
                )
        except Exception as e:
            logger.warning("Failed to write query log: %s", e)

    return reply
