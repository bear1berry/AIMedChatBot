# bot/ai_client.py
import logging
from typing import List, Dict, Any

import httpx

from .config import settings
from .modes import MODES, build_system_prompt
from .memory import save_dialog_turn, load_dialog_history


logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/v1/chat/completions"


def _build_messages(
    user_id: int,
    mode: str,
    user_message: str,
) -> List[Dict[str, Any]]:
    """
    Собираем сообщения для Groq:
    - system (под режим)
    - предыдущая история (если есть)
    - текущее user-сообщение
    """
    mode_cfg = MODES.get(mode, MODES["default"])
    system_prompt = build_system_prompt(mode_cfg)

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt}
    ]

    # История из памяти (если реализована)
    history = load_dialog_history(user_id=user_id, limit=10)
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})

    messages.append({"role": "user", "content": user_message})
    return messages


async def ask_ai(
    user_id: int,
    mode: str,
    user_message: str,
) -> str:
    """
    Главная функция обращения к Groq.
    Возвращает текст ответа или человекочитаемую ошибку.
    """
    if not settings.groq_api_key:
        logger.error("GROQ_API_KEY is not set")
        return "⚠️ Ошибка конфигурации: не задан GROQ_API_KEY."

    if not settings.model:
        logger.error("MODEL_NAME (settings.model) is not set")
        return "⚠️ Ошибка конфигурации: не задан MODEL_NAME (модель Groq)."

    messages = _build_messages(user_id, mode, user_message)
    mode_cfg = MODES.get(mode, MODES["default"])
    temperature = mode_cfg.get("temperature", 0.3)

    logger.info(
        "Sending request to Groq for user %s in mode %s with model %s",
        user_id, mode, settings.model,
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                GROQ_URL,
                headers={
                    "Authorization": f"Bearer {settings.groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.model,
                    "messages": messages,
                    "temperature": temperature,
                    "stream": False,
                },
            )

        # Если не 2xx — выбрасываем исключение
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Логируем тело ответа, чтобы видеть, что не так
            body = ""
            try:
                body = resp.text
            except Exception:
                pass

            logger.error(
                "Groq API error: %s %s, body=%r",
                e.response.status_code,
                e.response.reason_phrase,
                body,
            )

            # Красивое сообщение пользователю
            if e.response.status_code == 404:
                return (
                    "❌ Groq вернул 404.\n"
                    "Скорее всего указана неверная модель или endpoint.\n"
                    "Проверь переменную MODEL_NAME в настройках Render."
                )
            if e.response.status_code == 401:
                return (
                    "❌ Ошибка авторизации в Groq.\n"
                    "Проверь правильность GROQ_API_KEY."
                )

            return (
                f"❌ Ошибка Groq API ({e.response.status_code}).\n"
                "Проблема на стороне модели, попробуйте позже."
            )

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            logger.error("Groq response without choices: %r", data)
            return "⚠️ Не удалось получить ответ от модели."

        reply = choices[0]["message"]["content"]

        # Сохраняем ход диалога в память (если реализовано)
        try:
            save_dialog_turn(
                user_id=user_id,
                user_message=user_message,
                assistant_message=reply,
                mode=mode,
            )
        except Exception as e:
            logger.warning("Failed to save dialog turn: %s", e)

        return reply

    except Exception as e:
        logger.exception("Unexpected error while calling Groq: %s", e)
        return (
            "❌ Произошла внутренняя ошибка при обращении к модели.\n"
            "Попробуйте ещё раз чуть позже."
        )

