# bot/config.py
import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Settings:
    # Telegram
    bot_token: str = os.getenv("BOT_TOKEN", "")

    # Groq
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    # ⚠️ ВАЖНО: здесь указана реальная модель Groq
    model: str = os.getenv("MODEL_NAME", "llama-3.1-8b-instant")

    # Доступ к боту
    allowed_users: List[str] = field(
        default_factory=lambda: [
            u.strip()
            for u in os.getenv("ALLOWED_USERS", "").split(",")
            if u.strip()
        ]
    )
    admin_user: str = os.getenv("ADMIN_USER", "")

    # Логирование запросов к ИИ
    log_file: str = os.getenv("REQUEST_LOG_FILE", "logs/requests.log")


settings = Settings()
