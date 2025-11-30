import os
from dataclasses import dataclass


@dataclass
class Settings:
    bot_token: str = os.environ["BOT_TOKEN"]
    groq_api_key: str = os.environ["GROQ_API_KEY"]
    # если MODEL_NAME не задана – берем дефолтную рабочую модель
    model_name: str = os.getenv("MODEL_NAME", "openai/gpt-oss-20b")


settings = Settings()
