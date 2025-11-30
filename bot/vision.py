import base64
import logging
from typing import Optional

import httpx

from .config import settings

logger = logging.getLogger(__name__)

# Groq audio (Whisper)
GROQ_AUDIO_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# OpenAI endpoints (vision + image generation) — используются только если задан OPENAI_API_KEY
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_IMAGES_URL = "https://api.openai.com/v1/images"


def _has_openai() -> bool:
    return bool(settings.openai_api_key)


# === Анализ изображения (vision) =====================================


async def analyze_image(image_bytes: bytes) -> str:
    """
    Анализ изображения через OpenAI vision (если доступен).
    Если OPENAI_API_KEY не задан, возвращает заглушку.
    """
    if not _has_openai():
        return (
            "У меня пока нет доступа к vision-модели. "
            "Могу ответить только по тексту."
        )

    img_b64 = base64.b64encode(image_bytes).decode("ascii")

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Опиши, что видно на изображении, сделай акцент на медицинских деталях, "
                            "но не ставь диагноз и не давай лечения. Добавь дисклеймер, что это не "
                            "заменяет консультацию врача."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 500,
    }

    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(OPENAI_CHAT_URL, json=payload, headers=headers)

    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()
    logger.info("Vision answer length: %s", len(text))
    return text


# === Генерация изображения (DALL·E / gpt-image-1) =====================


async def generate_image(prompt: str) -> Optional[str]:
    """
    Генерация изображения по текстовому запросу.
    Возвращает URL картинки или None.
    """
    if not _has_openai():
        return None

    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
    }

    payload = {
        "model": "gpt-image-1",
        "prompt": prompt,
        "size": "1024x1024",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{OPENAI_IMAGES_URL}/generations", json=payload, headers=headers)

    resp.raise_for_status()
    data = resp.json()
    url = data["data"][0].get("url")
    logger.info("Generated image URL: %s", url)
    return url


# === Распознавание голосовых (Whisper через Groq) =====================


async def transcribe_audio(audio_bytes: bytes, filename: str = "audio.ogg") -> str:
    """
    Распознаёт голосовое сообщение с помощью Whisper large v3 на Groq.
    """
    files = {
        "file": (filename, audio_bytes, "audio/ogg"),
        "model": (None, settings.whisper_model),
        "response_format": (None, "json"),
        "temperature": (None, "0"),
    }

    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(GROQ_AUDIO_URL, files=files, headers=headers)

    resp.raise_for_status()
    data = resp.json()
    text = data.get("text", "").strip()
    logger.info("Whisper transcription length: %s", len(text))
    return text
