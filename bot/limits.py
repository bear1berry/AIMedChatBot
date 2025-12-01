from __future__ import annotations

import time
from typing import Optional, Tuple

from .config import settings

# Простое in-memory хранилище лимитов:
# user_id -> {minute_ts, minute_count, day_ts, day_count}
_RATE_STATE: dict[int, dict[str, int]] = {}


def check_rate_limit(user_id: int) -> Tuple[bool, Optional[int], Optional[str]]:
    """
    Проверка лимитов:
    - N запросов в минуту
    - M запросов в сутки

    Возвращает:
        (ok, retry_after, message)
    """
    now = int(time.time())
    minute = now // 60
    day = now // (24 * 60 * 60)

    bucket = _RATE_STATE.get(user_id)
    if bucket is None:
        bucket = {
            "minute_ts": minute,
            "minute_count": 0,
            "day_ts": day,
            "day_count": 0,
        }
        _RATE_STATE[user_id] = bucket

    # Сбрасываем окна при смене минуты/дня
    if bucket["minute_ts"] != minute:
        bucket["minute_ts"] = minute
        bucket["minute_count"] = 0
    if bucket["day_ts"] != day:
        bucket["day_ts"] = day
        bucket["day_count"] = 0

    # Проверка минутного лимита
    if bucket["minute_count"] >= settings.rate_limit_per_minute:
        retry = (bucket["minute_ts"] + 1) * 60 - now
        msg = (
            "⏳ Лимит запросов в минуту превышен.\n"
            "Попробуй отправить сообщение чуть позже."
        )
        return False, retry, msg

    # Проверка дневного лимита
    if bucket["day_count"] >= settings.rate_limit_per_day:
        retry = (bucket["day_ts"] + 1) * 24 * 60 * 60 - now
        msg = (
            "🚫 Достигнут дневной лимит запросов для этого бота.\n"
            "Лимит обновится завтра."
        )
        return False, retry, msg

    # Учитываем запрос
    bucket["minute_count"] += 1
    bucket["day_count"] += 1
    return True, None, None
