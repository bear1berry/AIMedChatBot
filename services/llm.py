from __future__ import annotations

from typing import Any, Dict, List

import httpx

from bot.config import DEEPSEEK_API_KEY, DEEPSEEK_API_BASE_URL, DEEPSEEK_MODEL


async def generate_answer(
    system_prompt: str,
    history: List[Dict[str, str]],
    user_message: str,
    timeout: float = 60.0,
) -> str:
    """
    Вызывает DeepSeek Chat Completions (OpenAI-совместимый API) и возвращает один текстовый ответ.
    """
    messages: List[Dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{DEEPSEEK_API_BASE_URL.rstrip('/')}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": messages,
                "stream": False,
            },
        )
        resp.raise_for_status()
        data: Dict[str, Any] = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("DeepSeek API вернул пустой список choices")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        return str(content).strip()
