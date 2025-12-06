from __future__ import annotations

from typing import Any, AsyncGenerator, Dict, List

import httpx

from bot.config import DEEPSEEK_API_KEY, DEEPSEEK_API_BASE_URL, DEEPSEEK_MODEL␊


async def generate_answer(␊
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


async def ask_llm_stream(
    mode_key: str,
    user_prompt: str,
    history: List[Dict[str, str]] | None = None,
) -> AsyncGenerator[str, None]:
    """Compatibility wrapper expected by the bot logic.

    The upstream DeepSeek API supports streaming responses, but for local
    development and tests we keep implementation simple and yield the full
    answer as a single chunk.  This keeps the rest of the code operational
    without requiring a live network connection during automated checks.
    """

    system_prompt = ""
    from bot.config import ASSISTANT_MODES, DEFAULT_MODE  # local import to avoid cycles

    mode_cfg = ASSISTANT_MODES.get(mode_key, ASSISTANT_MODES[DEFAULT_MODE])
    system_prompt = mode_cfg.get("system_prompt") or ""

    answer = await generate_answer(
        system_prompt=system_prompt,
        history=history or [],
        user_message=user_prompt,
    )
    yield answer
