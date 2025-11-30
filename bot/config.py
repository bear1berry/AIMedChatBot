from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

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


def _env_int_list(name: str) -> List[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    result: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


@dataclass
class Settings:
    # Telegram
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", "").strip())
    admin_ids: List[int] = field(default_factory=lambda: _env_int_list("ADMIN_IDS"))
    allowed_users: List[str] = field(default_factory=list)

    # Groq / LLM
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", "").strip())
    groq_chat_model: str = field(
        default_factory=lambda: os.getenv("MODEL_NAME", "openai/gpt-oss-120b").strip()
    )
    groq_chat_model_light: str = field(
        default_factory=lambda: os.getenv("GROQ_SECONDARY_MODEL", "").strip()
    )
    groq_vision_model: str = field(
        default_factory=lambda: os.getenv("GROQ_VISION_MODEL", "llama-3.2-11b-vision-preview").strip()
    )

    # Storage
    db_path: str = field(
        default_factory=lambda: os.getenv("DB_PATH", "./bot_data.sqlite3").strip() or "./bot_data.sqlite3"
    )

    # Channel posting (опционально, пока только для будущих фич)
    post_channel_id: int | None = field(
        default_factory=lambda: _env_int("POST_CHANNEL_ID", 0) or None
    )

    # Rate limiting
    rate_limit_per_minute: int = field(default_factory=lambda: _env_int("RATE_LIMIT_PER_MINUTE", 20))
    rate_limit_per_day: int = field(default_factory=lambda: _env_int("RATE_LIMIT_PER_DAY", 500))

    def __post_init__(self) -> None:
        allowed_raw = os.getenv("ALLOWED_USERNAMES", "").strip()
        if allowed_raw:
            self.allowed_users = [u.strip().lstrip("@") for u in allowed_raw.split(",") if u.strip()]
        else:
            self.allowed_users = []

        # Лёгкая модель по умолчанию = основная, если не указана явно
        if not self.groq_chat_model_light:
            self.groq_chat_model_light = self.groq_chat_model


settings = Settings()
