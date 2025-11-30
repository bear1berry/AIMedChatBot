# bot/config.py
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # Telegram
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", "").strip())

    # Groq
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", "").strip())
    groq_base_url: str = field(
        default_factory=lambda: os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    )
    groq_chat_model: str = field(
        default_factory=lambda: os.getenv("GROQ_CHAT_MODEL", "llama-3.1-70b-versatile")
    )
    groq_vision_model: str = field(
        default_factory=lambda: os.getenv("GROQ_VISION_MODEL", "llama-3.2-11b-vision-preview")
    )

    # Файлы
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "bot.log"))
    query_log_file: str = field(default_factory=lambda: os.getenv("QUERY_LOG_FILE", "queries.log"))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "bot.db"))

    # Доступ / админка
    allowed_users: list[str] = field(default_factory=list)
    admin_username: str | None = field(default_factory=lambda: os.getenv("ADMIN_USERNAME"))

    def __post_init__(self) -> None:
        allowed = os.getenv("ALLOWED_USERNAMES", "").strip()
        if allowed:
            self.allowed_users = [
                u.strip().lstrip("@")
                for u in allowed.split(",")
                if u.strip()
            ]
        else:
            self.allowed_users = []


settings = Settings()
