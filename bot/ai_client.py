import httpx
from .config import settings

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# основная безопасная модель
DEFAULT_MODEL = "openai/gpt-oss-20b"

SYSTEM_PROMPT = (
    "Ты — ИИ-помощник медицинского Telegram-канала AI.Medicine.\n"
    "Отвечай коротко, понятным языком для неспециалиста.\n"
    "Не ставь диагнозы, не назначай лечение, не давай индивидуальных "
    "медицинских рекомендаций.\n"
    "Всегда подчеркивай, что информация носит ознакомительный характер "
    "и не заменяет очную консультацию врача."
)


async def ask_ai(user_message: str) -> str:
    """
    Отправляет запрос к Groq API и возвращает текст ответа.
    Есть fallback: если выбранная модель не найдена/выведена из оборота –
    один раз пробуем DEFAULT_MODEL.
    """

    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }

    async def _request(model_name: str) -> tuple[int, dict]:
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": 600,
            "temperature": 0.6,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(GROQ_API_URL, headers=headers, json=payload)
        try:
            data = resp.json()
        except Exception:
            data = {}
        return resp.status_code, data

    # модель из окружения или дефолт
    model_to_use = settings.model_name or DEFAULT_MODEL

    try:
        status, data = await _request(model_to_use)
    except httpx.RequestError:
        return "Ошибка при обращении к ИИ. Попробуй ещё раз чуть позже."

    # Если модель не найдена / деактивирована – пробуем дефолтную
    if (
        status == 404
        and isinstance(data, dict)
        and data.get("error", {}).get("code") in {"model_not_found", "model_decommissioned"}
        and model_to_use != DEFAULT_MODEL
    ):
        status, data = await _request(DEFAULT_MODEL)

    # Обработка ошибок API
    if status >= 400:
        error_msg = None
        if isinstance(data, dict):
            error = data.get("error") or {}
            error_msg = error.get("message")

        if error_msg:
            # Можно логировать error_msg, а пользователю показывать мягкую фразу
            return f"Groq API error: {error_msg}"
        return "Не удалось получить ответ от ИИ (ошибка сервера). Попробуй ещё раз позже."

    # Достаем текст ответа
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return "Мне не удалось сформировать ответ. Попробуй переформулировать вопрос."

    return content.strip()
