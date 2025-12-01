from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value.strip()


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
    bot_token: str = field(default_factory=lambda: _env_required("BOT_TOKEN"))

    # Groq
    groq_api_key: str = field(default_factory=lambda: _env_required("GROQ_API_KEY"))
    model_name: str = field(
        default_factory=lambda: os.getenv("MODEL_NAME", "openai/gpt-oss-120b")
    )

    # Rate limiting
    rate_limit_per_minute: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_PER_MINUTE", 20)
    )
    rate_limit_per_day: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_PER_DAY", 500)
    )

    # Список разрешённых юзернеймов, если нужно ограничить доступ
    allowed_users: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        allowed_raw = os.getenv("ALLOWED_USERNAMES", "").strip()
        if allowed_raw:
            self.allowed_users = [
                u.strip().lstrip("@") for u in allowed_raw.split(",") if u.strip()
            ]
        else:
            self.allowed_users = []


settings = Settings()
