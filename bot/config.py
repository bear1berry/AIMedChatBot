from dataclasses import dataclass, field
import os


@dataclass
class Settings:
    # Ключи и токены
    bot_token: str = os.getenv("BOT_TOKEN", "")
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")

    # Модель Groq (рабочая)
    model_name: str = os.getenv("MODEL_NAME", "llama-3.1-70b-versatile")

    # Доступные пользователи (исправлено!)
    allowed_users: list[str] = field(default_factory=lambda: [
        "bear1berry",
        "AraBysh"
    ])

    # Админ
    admin_user: str = "bear1berry"


settings = Settings()
