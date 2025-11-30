import httpx
from .config import GROQ_API_KEY, MODEL_NAME

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = (
    "Ты — ИИ-помощник в медицинском Telegram-канале. "
    "Отвечай на вопросы пользователей в формате просветительской информации: "
    "объясняй термины, процессы, общие подходы к диагностике и лечению, "
    "но НЕ ставь диагнозы, НЕ назначай лечение, не давай индивидуальных "
    "медицинских рекомендаций. Всегда добавляй дисклеймер о том, что это не "
    "является медицинской консультацией и необходимо очно обращаться к врачу."
)


async def ask_ai(user_message: str) -> str:
    """
    Отправляет запрос к Groq Chat Completions и возвращает текст ответа.
    """
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.4,
        "max_tokens": 700,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(GROQ_API_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    try:
        content = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        content = "Не удалось получить корректный ответ от ИИ. Попробуйте ещё раз позже."

    return content
