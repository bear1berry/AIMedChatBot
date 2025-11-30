import os
from dataclasses import dataclass


@dataclass
class Settings:
    bot_token: str = os.environ["BOT_TOKEN"]
    groq_api_key: str = os.environ["GROQ_API_KEY"]
    model_name: str = os.getenv("MODEL_NAME", "openai/gpt-oss-20b")

    # список разрешённых пользователей
    allowed_users: list[str] = os.getenv(
        "ALLOWED_USERS",
        "bear1berry,AraBysh"
    ).split(",")

    # админ (строго один)
    admin_user: str = os.getenv("ADMIN_USER", "bear1berry")
    

settings = Settings()
