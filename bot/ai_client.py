from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from .config import settings
from .modes import build_system_prompt
from .memory import get_history, save_message

logger = logging.getLogger(__name__)


async def ask_ai(user_id: int, mode: str, user_message: str) -> str:
    """
    Основной запрос к Groq / gpt-oss-120b.
    """
    if not settings.groq_api_key:
        return (
            "❗️ Не настроен ключ GROQ_API_KEY.\n"
            "Добавь его в переменные окружения на Render и перезапусти сервис."
        )

    system_prompt = build_system_prompt(user_id, mode)
    history = get_history(user_id, limit=12)

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for m in history:
        # В БД роли только 'user'/'assistant'
        role = m["role"]
        if role not in ("user", "assistant"):
            role = "user"
        messages.append({"role": role, "content": m["content"]})

    messages.append({"role": "user", "content": user_message})

    url = settings.groq_base_url + "/chat/completions"

    logger.info(
        "Sending request to Groq model=%s user=%s mode=%s",
        settings.groq_chat_model,
        user_id,
        mode,
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
                    "temperature": 0.35,
                    "max_tokens": 1200,
                },
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = e.response.text if e.response is not None else ""
            logger.error(
                "HTTP error from Groq: %s status=%s body=%s",
                e,
                e.response.status_code if e.response else "unknown",
                body,
            )
            return (
                f"❗️ Ошибка при обращении к модели ({e.response.status_code if e.response else '??'}).\n"
                f"URL: {url}\n"
                f"Ответ сервера: {body[:800]}"
            )

        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.exception("Unhandled error calling Groq: %s", e)
        return f"❗️ Внутренняя ошибка при запросе к модели: {e}"

    # Логируем в БД
    save_message(user_id, "user", user_message, mode)
    save_message(user_id, "assistant", reply, mode)

    # При желании — лог в файл
    if settings.query_log_file:
        try:
            with open(settings.query_log_file, "a", encoding="utf-8") as f:
                f.write(
                    f"\n=== USER {user_id} MODE {mode} ===\n"
                    f"Q: {user_message}\n\nA: {reply}\n"
                    "-----------------------------\n"
                )
        except Exception as e:
            logger.warning("Failed to write query log: %s", e)

    return reply
