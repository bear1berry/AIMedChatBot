from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Tuple

import httpx

from .config import settings
from .modes import build_system_prompt
from .memory import get_history, save_message

logger = logging.getLogger(__name__)

_http_client: httpx.AsyncClient | None = None


def _get_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=10.0,
        read=40.0,
        write=10.0,
        pool=None,
    )


def _get_limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=50,
        max_keepalive_connections=20,
    )


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=_get_timeout(),
            limits=_get_limits(),
        )
        logger.info("HTTP client for Groq initialized")
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
        logger.info("HTTP client for Groq closed")


async def ask_ai(user_id: int, mode: str, user_message: str) -> str:
    if not settings.groq_api_key:
        return (
            "❗️ Не настроен ключ GROQ_API_KEY.\n"
            "Добавь его в переменные окружения и перезапусти сервис."
        )

    system_prompt = build_system_prompt(user_id, mode)
    history = get_history(user_id, limit=12)

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    for m in history:
        role = m["role"]
        if role not in ("user", "assistant"):
            role = "user"
        messages.append({"role": role, "content": m["content"]})

    messages.append({"role": "user", "content": user_message})

    url = settings.groq_base_url + "/chat/completions"
    client = await get_http_client()

    logger.info(
        "Sending request to Groq model=%s user=%s mode=%s",
        settings.groq_chat_model,
        user_id,
        mode,
    )

    max_attempts = 3
    last_error_text: str | None = None

    for attempt in range(1, max_attempts + 1):
        try:
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

            resp.raise_for_status()
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
            break

        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else None
            body = e.response.text if e.response is not None else ""
            last_error_text = body
            logger.error(
                "HTTP error from Groq (attempt %d/%d): status=%s body=%s",
                attempt,
                max_attempts,
                status,
                body[:500],
            )

            if status in (429, 500, 502, 503, 504) and attempt < max_attempts:
                await asyncio.sleep(1.5 * attempt)
                continue

            return (
                f"❗️ Ошибка при обращении к модели (HTTP {status}).\n"
                f"Ответ сервера: {body[:800]}"
            )

        except httpx.RequestError as e:
            last_error_text = str(e)
            logger.warning(
                "Network error talking to Groq (attempt %d/%d): %s",
                attempt,
                max_attempts,
                e,
            )
            if attempt < max_attempts:
                await asyncio.sleep(1.5 * attempt)
                continue
            return (
                "❗️ Не удалось связаться с моделью (сетевой сбой).\n"
                "Попробуй ещё раз чуть позже."
            )

        except Exception as e:
            last_error_text = str(e)
            logger.exception("Unhandled error calling Groq on attempt %d/%d: %s", attempt, max_attempts, e)
            if attempt < max_attempts:
                await asyncio.sleep(1.5 * attempt)
                continue
            return f"❗️ Внутренняя ошибка при запросе к модели: {e!r}"

    else:
        return (
            "❗️ Не удалось получить ответ от модели после нескольких попыток.\n"
            f"Последняя ошибка: {last_error_text}"
        )

    save_message(user_id, "user", user_message, mode)
    save_message(user_id, "assistant", reply, mode)

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


async def healthcheck_llm() -> Tuple[bool, str]:
    """
    Лёгкий ping модели для /ping, без логирования в диалог.
    """
    if not settings.groq_api_key:
        return False, "❌ GROQ_API_KEY не настроен"

    client = await get_http_client()
    url = settings.groq_base_url + "/chat/completions"

    messages = [{"role": "user", "content": "ping"}]
    payload = {
        "model": settings.groq_chat_model,
        "messages": messages,
        "max_tokens": 4,
        "temperature": 0.0,
    }

    start = time.monotonic()
    try:
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return True, f"✅ Модель отвечает (≈{elapsed_ms} мс)"
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else None
        return False, f"❌ Модель недоступна (HTTP {status})"
    except httpx.RequestError as e:
        return False, f"❌ Сетевая ошибка при обращении к модели: {e}"
    except Exception as e:
        return False, f"❌ Внутренняя ошибка healthcheck модели: {e!r}"
