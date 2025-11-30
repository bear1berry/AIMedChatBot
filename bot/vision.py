import httpx
import base64

from .config import settings

VISION_MODEL = "llava-v1.5-7b"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


async def analyze_image(image_bytes: bytes) -> str:
    """
    Анализ изображения с помощью Groq LLaVA Vision.
    """

    try:
        # Кодируем изображение в base64
        img_b64 = base64.b64encode(image_bytes).decode("utf-8")

        payload = {
            "model": VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Проанализируй изображение и опиши, что на нём изображено."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
                        }
                    ]
                }
            ],
            "max_tokens": 300
        }

        headers = {
            "Authorization": f"Bearer {settings.groq_api_key}",
            "Content-Type": "application/json"
        }

        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(GROQ_URL, json=payload, headers=headers)
            data = r.json()

        if "choices" in data:
            return data["choices"][0]["message"]["content"]

        return f"Ошибка Groq Vision: {data}"

    except Exception as e:
        return f"Ошибка обработки изображения: {e}"
