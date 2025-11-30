from __future__ import annotations

import time
from typing import Optional, Tuple

from .config import settings
from .memory import _get_conn  # используем внутренний коннектор к БД


def check_rate_limit(user_id: int) -> Tuple[bool, Optional[int], Optional[str]]:
    """
    Простой rate limit:
    - N запросов в минуту
    - M запросов в сутки
    Хранится в таблице rate_limits (user_id, scope, window_start, count).

    Возвращает:
      (ok, retry_after, message)
    """
    per_min = settings.rate_limit_per_minute
    per_day = settings.rate_limit_per_day

    # Лимиты отключены
    if per_min <= 0 and per_day <= 0:
        return True, None, None

    conn = _get_conn()
    cur = conn.cursor()
    now = int(time.time())

    limits = [
        ("minute", per_min, 60),
        ("day", per_day, 86400),
    ]

    blocked_retry: Optional[int] = None

    for scope, limit, window in limits:
        if limit <= 0:
            continue

        cur.execute(
            "SELECT window_start, count FROM rate_limits WHERE user_id = ? AND scope = ?",
            (user_id, scope),
        )
        row = cur.fetchone()

        if not row:
            # первая запись
            cur.execute(
                "INSERT OR REPLACE INTO rate_limits (user_id, scope, window_start, count) "
                "VALUES (?, ?, ?, ?)",
                (user_id, scope, now, 1),
            )
            continue

        start = int(row["window_start"])
        count = int(row["count"])

        # окно истекло — начинаем заново
        if now - start >= window:
            cur.execute(
                "UPDATE rate_limits SET window_start = ?, count = ? WHERE user_id = ? AND scope = ?",
                (now, 1, user_id, scope),
            )
            continue

        # лимит исчерпан
        if count >= limit:
            retry = window - (now - start)
            if blocked_retry is None or retry > blocked_retry:
                blocked_retry = retry
        else:
            # увеличиваем счётчик
            cur.execute(
                "UPDATE rate_limits SET count = count + 1 WHERE user_id = ? AND scope = ?",
                (user_id, scope),
            )

    conn.commit()
    cur.close()

    if blocked_retry is not None:
        # Показываем более «красивое» сообщение
        if blocked_retry >= 60:
            minutes = blocked_retry // 60
            msg = f"⏳ Лимит запросов превышен. Попробуй снова примерно через {minutes} мин."
        else:
            msg = f"⏳ Лимит запросов превышен. Попробуй снова через {blocked_retry} сек."
        return False, blocked_retry, msg

    return True, None, None
