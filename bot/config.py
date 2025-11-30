import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    # Telegram
    bot_token: str = os.environ["BOT_TOKEN"]

    # Groq (текст + Whisper)
    groq_api_key: str = os.environ["GROQ_API_KEY"]
    model_name: str = os.environ.get("MODEL_NAME", "gpt-oss-20b")
    whisper_model: str = os.environ.get("WHISPER_MODEL", "whisper-large-v3")

    # OpenAI (опционально: DALL·E / vision)
    openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")

    # SQLite
    db_path: str = os.environ.get("DB_PATH", "bot.db")

    # Доступ по username
    allowed_users: list[str] = field(
        default_factory=lambda: ["bear1berry", "AraBysh"]
    )
    admin_user: str = os.environ.get("ADMIN_USER", "bear1berry")


settings = Settings()
