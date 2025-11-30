# bot/config.py
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get_groq_chat_model() -> str:
    """
    Определяем модель чата Groq с учётом разных имён переменных окружения.
    Приоритет:
    1) GROQ_CHAT_MODEL
    2) MODEL_NAME (как в Render на скриншоте)
    3) Безопасный дефолт: llama-3.1-8b-instant
    """
    env_model = (
        os.getenv("GROQ_CHAT_MODEL")
        or os.getenv("MODEL_NAME")
        or ""
    ).strip()

    if env_model:
        return env_model

    # Дефолтная модель — быстрая и дёшево обходится
    return "llama-3.1-8b-instant"


def _get_groq_vision_model() -> str:
    """
    Модель для vision-запросов.
    Можно переопределить через GROQ_VISION_MODEL.
    """
    env_model = (os.getenv("GROQ_VISION_MODEL") or "").strip()
    if env_model:
        return env_model
    return "llama-3.2-11b-vision-preview"


@dataclass
class Settings:
    # Telegram
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", "").strip())

    # Groq
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", "").strip())
    groq_base_url: str = field(
        default_factory=lambda: os.getenv(
            "GROQ_BASE_URL",
            "https://api.groq.com/openai/v1",
        ).rstrip("/")
    )
    groq_chat_model: str = field(default_factory=_get_groq_chat_model)
    groq_vision_model: str = field(default_factory=_get_groq_vision_model)

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
