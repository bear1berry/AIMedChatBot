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
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL_NAME,
                "messages": [
                    {"role": "user", "content": user_message}
                ],
                "max_tokens": 500,
                "temperature": 0.7
            }
        )

    data = r.json()

    return data["choices"][0]["message"]["content"]


