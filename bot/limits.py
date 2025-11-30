from __future__ import annotations

import time
from typing import Optional, Tuple

from .config import settings
from .memory import _get_conn  # внутренний коннектор к БД


def check_rate_limit(user_id: int) -> Tuple[bool, Optional[int], Optional[str]]:
    """
    Лимиты для пользователя на SQLite:
    - N запросов в минуту
    - M запросов в сутки

    Возвращает:
        ok: bool
        retry_after: секунды до следующей попытки (если заблокирован)
        message: человекочитаемое сообщение (если заблокирован)
    """
    now = int(time.time())
    conn = _get_conn()
    cur = conn.cursor()

    scopes = [
        ("minute", 60, settings.rate_limit_per_minute),
        ("day", 24 * 60 * 60, settings.rate_limit_per_day),
    ]

    blocked_retry: Optional[int] = None
    blocked_msg: Optional[str] = None

    for scope, window_size, limit in scopes:
        if limit <= 0:
            continue

        window_start = now - (now % window_size)

        cur.execute(
            "SELECT window_start, count FROM rate_limits WHERE user_id = ? AND scope = ?",
            (user_id, scope),
        )
        row = cur.fetchone()

        if row is None:
            cur.execute(
                "INSERT INTO rate_limits (user_id, scope, window_start, count) VALUES (?, ?, ?, ?)",
                (user_id, scope, window_start, 0),
            )
            current_start = window_start
            current_count = 0
        else:
            current_start, current_count = int(row["window_start"]), int(row["count"])
            if current_start != window_start:
                current_start = window_start
                current_count = 0
                cur.execute(
                    "UPDATE rate_limits SET window_start = ?, count = ? WHERE user_id = ? AND scope = ?",
                    (current_start, current_count, user_id, scope),
                )

        if current_count >= limit:
            retry = current_start + window_size - now
            if blocked_retry is None or retry > blocked_retry:
                blocked_retry = max(retry, 1)
                if scope == "minute":
                    blocked_msg = (
                        "⏳ Ты отправляешь слишком много запросов за короткое время. "
                        "Попробуй ещё раз через несколько секунд."
                    )
                else:
                    blocked_msg = (
                        "🚦 Достигнут дневной лимит запросов для этого бота. "
                        "Лимит обновится завтра."
                    )

    if blocked_retry is not None:
        cur.close()
        return False, blocked_retry, blocked_msg

    # не заблокирован — инкремент счётчиков
    for scope, _, limit in scopes:
        if limit <= 0:
            continue
        cur.execute(
            "UPDATE rate_limits SET count = count + 1 WHERE user_id = ? AND scope = ?",
            (user_id, scope),
        )

    conn.commit()
    cur.close()
    return True, None, None
