from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    # Telegram
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", "").strip())

    # Groq (OpenAI-compatible API)
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", "").strip())
    groq_base_url: str = field(
        default_factory=lambda: os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    )

    # Chat model — по умолчанию мощный openai/gpt-oss-120b
    groq_chat_model: str = field(
        default_factory=lambda: os.getenv("GROQ_CHAT_MODEL", "openai/gpt-oss-120b").strip()
    )

    # Vision model
    groq_vision_model: str = field(
        default_factory=lambda: os.getenv("GROQ_VISION_MODEL", "llama-3.2-11b-vision-preview").strip()
    )

    # Storage
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "bot.db"))

    # Admin / access control
    admin_username: str | None = field(default_factory=lambda: os.getenv("ADMIN_USERNAME") or None)
    allowed_users: list[str] = field(default_factory=list)

    # Logging
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "bot.log"))
    query_log_file: str = field(default_factory=lambda: os.getenv("QUERY_LOG_FILE", "queries.log"))

    # Rate limiting
    rate_limit_per_minute: int = field(default_factory=lambda: _env_int("RATE_LIMIT_PER_MINUTE", 20))
    rate_limit_per_day: int = field(default_factory=lambda: _env_int("RATE_LIMIT_PER_DAY", 500))

    def __post_init__(self) -> None:
        allowed_raw = os.getenv("ALLOWED_USERNAMES", "").strip()
        if allowed_raw:
            self.allowed_users = [u.strip().lstrip("@") for u in allowed_raw.split(",") if u.strip()]
        else:
            self.allowed_users = []


settings = Settings()
