import httpx
from .config import GROQ_API_KEY, MODEL_NAME

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

SYSTEM_PROMPT = (
    "Ты — ИИ-помощник в медицинском Telegram-канале. "
    "Отвечай на вопросы пользователей в формате просветительской информации: "
    "объясняй термины, процессы, общие подходы к диагностике и лечению, "
    "но НЕ ставь диагнозы, не назначай лечение, не давай индивидуальных "
    "медицинских рекомендаций. Всегда добавляй дисклеймер о том, что это не "
    "является медицинской консультацией и необходимо очно обратиться к врачу."
)


async def ask_ai(user_message: str) -> str:
    """
    Главная функция — отправка запроса в Groq и получение ответа.
    """

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ],
        "max_tokens": 600,
        "temperature": 0.7
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.post(
                GROQ_API_URL,
                headers=headers,
                json=payload
            )

        # Бросить исключение, если ошибка уровня HTTP
        response.raise_for_status()

        data = response.json()
        return data["choices"][0]["message"]["content"]

    except httpx.HTTPStatusError as e:
        # Ошибка Groq (400, 401, 404, 500)
        return f"Groq API error: {e.response.status_code}\n{e.response.text}"

    except Exception as e:
        # Любая другая ошибка
        return f"⚠ Ошибка при запросе к ИИ: {e}"
