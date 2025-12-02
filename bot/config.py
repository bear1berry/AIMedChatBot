from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env_required(name: str) -> str:
    """
    Получить обязательную переменную окружения.
    Если переменная не задана — бросаем понятную ошибку при старте бота.
    """
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}")


@dataclass
class Settings:
    # Telegram
    bot_token: str = field(default_factory=lambda: _env_required("BOT_TOKEN"))

    # Yandex Cloud / YandexGPT
    # Ключ и каталог для доступа к YandexGPT через OpenAI-совместимый API.
    yandex_api_key: str = field(
        default_factory=lambda: _env_required("YANDEX_CLOUD_API_KEY")
    )
    yandex_folder_id: str = field(
        default_factory=lambda: _env_required("YANDEX_CLOUD_FOLDER")
    )
    # Имя модели только для информации, логика выбора модели живёт в ai_client.
    model_name: str = field(
        default_factory=lambda: os.getenv(
            "MODEL_NAME",
            "gpt://{folder}/yandexgpt/latest".format(
                folder=os.getenv("YANDEX_CLOUD_FOLDER", "<folder_id>")
            ),
        )
    )

    # Путь к базе данных для хранения заметок/истории
    db_path: str = field(
        default_factory=lambda: os.getenv("DB_PATH", "bot_data.sqlite3")
    )

    # Rate limiting для вспомогательных механизмов (limits.py)
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
