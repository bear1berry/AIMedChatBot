import base64
import logging
import httpx
from .config import settings
from .modes import MODES, build_system_prompt
from .memory import get_history, save_message

# ------------------------------------------------------------
#  Настройка логов
# ------------------------------------------------------------
logger = logging.getLogger("bot.ai_client")
handler = logging.FileHandler("logs/ai.log", encoding="utf-8")
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ------------------------------------------------------------
#  Правильный API endpoint Groq
# ------------------------------------------------------------
GROQ_URL = "https://api.groq.com/v1/chat/completions"

# ------------------------------------------------------------
#  Модели с приоритетами
# ------------------------------------------------------------
PRIMARY_MODEL = "llama-3.1-70b-versatile"
FALLBACK_MODELS = [
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
]


# ------------------------------------------------------------
#  Универсальный отправщик запросов
# ------------------------------------------------------------
async def _send_request(payload):
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(GROQ_URL, json=payload, headers=headers)
        logger.info(f"Groq response code: {resp.status_code}")
        logger.debug(f"Groq response body: {resp.text}")

        resp.raise_for_status()
        return resp.json()


# ------------------------------------------------------------
#  Анализ изображений через Vision-модель Groq
# ------------------------------------------------------------
def _encode_image(image_bytes: bytes) -> str:
    """Преобразуем изображение в base64."""
    return base64.b64encode(image_bytes).decode("utf-8")


async def ask_vision(prompt_text: str, image_bytes: bytes) -> str:
    """Vision режим — LLaMA3 Vision."""
    encoded = _encode_image(image_bytes)

    payload = {
        "model": "llama-3.2-90b-vision-preview",
        "messages": [
            {"role": "system", "content": "You are a medical vision analysis assistant."},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt_text},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}"}
                ],
            }
        ]
    }

    response = await _send_request(payload)
    return response["choices"][0]["message"]["content"]


# ------------------------------------------------------------
#  Генерация изображений (эмуляция через текст)
#  Groq не имеет DALL·E — делаем безопасный адаптер
# ------------------------------------------------------------
async def ask_image_generation(prompt: str) -> str:
    """Эмуляция генерации картинки — Groq НЕ поддерживает DALL·E.
       Мы генерируем текстовое описание и даём ссылку для Midjourney/Flux."""
    
    payload = {
        "model": PRIMARY_MODEL,
        "messages": [
            {"role": "system", "content": "You are an AI that converts prompts into perfect image-generation prompts."},
            {"role": "user", "content": prompt}
        ]
    }

    response = await _send_request(payload)
    text = response["choices"][0]["message"]["content"]

    return f"🎨 <b>Готово!</b>\nВот идеальный промпт для генерации изображения:\n\n<code>{text}</code>"


# ------------------------------------------------------------
#  Основной обработчик диалога
# ------------------------------------------------------------
async def ask_ai(user_id: int, mode: str, user_message: str) -> str:
    """
    Основная функция общения с Groq.
    Поддерживает режимы, историю и fallback.
    """

    logger.info(f"Sending request to Groq for user {user_id} in mode {mode}")

    system_prompt = build_system_prompt(mode)
    history = get_history(user_id)

    messages = [{"role": "system", "content": system_prompt}]

    # История диалога
    for h_role, h_text in history:
        messages.append({"role": h_role, "content": h_text})

    # Новое сообщение пользователя
    messages.append({"role": "user", "content": user_message})

    # Подготовка основного payload
    def build_payload(model_name):
        return {
            "model": model_name,
            "messages": messages,
            "max_tokens": 2048,
            "temperature": 0.4,
            "top_p": 0.95,
        }

    # ------------------------------------------------------------
    #  Попытка №1 — PRIMARY_MODEL
    # ------------------------------------------------------------
    models_to_try = [PRIMARY_MODEL] + FALLBACK_MODELS

    for model in models_to_try:
        try:
            payload = build_payload(model)
            response = await _send_request(payload)

            reply_text = response["choices"][0]["message"]["content"]

            # Сохраняем в БД
            save_message(user_id, "user", user_message)
            save_message(user_id, "assistant", reply_text)

            return reply_text

        except Exception as e:
            logger.error(f"Model {model} failed: {e}")
            continue

    return "❌ Ошибка: ни одна модель Groq не ответила. Попробуйте позже."


# ------------------------------------------------------------
#  Экспорт наружу
# ------------------------------------------------------------
__all__ = [
    "ask_ai",
    "ask_vision",
    "ask_image_generation"
]
