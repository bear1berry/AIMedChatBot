from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Пытаемся переиспользовать тот же SQLite, что и Storage
try:
    from services.storage import DB_PATH as STORAGE_DB_PATH  # type: ignore[attr-defined]
    METRICS_DB_PATH = Path(STORAGE_DB_PATH)
except Exception:
    # Фолбэк — отдельный файл, если вдруг DB_PATH не задан
    METRICS_DB_PATH = Path("aimedbot.db")

_INITIALIZED = False


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(METRICS_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema() -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS metrics_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL NOT NULL,
            user_id      INTEGER,
            event_type   TEXT NOT NULL,  -- 'chat_turn', 'limit_hit', 'invoice_created', 'invoice_status'
            intent_type  TEXT,
            mode_key     TEXT,
            request_len  INTEGER,
            response_len INTEGER,
            plan_code    TEXT,
            tariff_key   TEXT,
            invoice_id   INTEGER,
            status       TEXT,
            extra_json   TEXT
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_metrics_events_type_ts "
        "ON metrics_events(event_type, ts)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_metrics_events_user_ts "
        "ON metrics_events(user_id, ts)"
    )
    conn.commit()
    conn.close()
    _INITIALIZED = True


def _insert_event(
    *,
    event_type: str,
    user_id: Optional[int] = None,
    intent_type: Optional[str] = None,
    mode_key: Optional[str] = None,
    request_len: Optional[int] = None,
    response_len: Optional[int] = None,
    plan_code: Optional[str] = None,
    tariff_key: Optional[str] = None,
    invoice_id: Optional[int] = None,
    status: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    _ensure_schema()
    ts = time.time()
    extra = extra or {}
    payload = {
        "ts": ts,
        "user_id": user_id,
        "event_type": event_type,
        "intent_type": intent_type,
        "mode_key": mode_key,
        "request_len": request_len,
        "response_len": response_len,
        "plan_code": plan_code,
        "tariff_key": tariff_key,
        "invoice_id": invoice_id,
        "status": status,
        "extra": extra,
    }

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO metrics_events (
            ts,
            user_id,
            event_type,
            intent_type,
            mode_key,
            request_len,
            response_len,
            plan_code,
            tariff_key,
            invoice_id,
            status,
            extra_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            user_id,
            event_type,
            intent_type,
            mode_key,
            request_len,
            response_len,
            plan_code,
            tariff_key,
            invoice_id,
            status,
            json.dumps(extra, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()

    # Структурный лог в текстовый лог — удобно парсить потом
    try:
        logger.info("metrics_event %s", json.dumps(payload, ensure_ascii=False))
    except Exception:
        # Логирование метрик не должно ломать бота
        logger.debug("Failed to json-log metrics_event", exc_info=True)


# ----------------------- Интенты -----------------------


def _detect_intent(text: str) -> str:
    """
    Очень лёгкий эвристический детектор типа интента:
    - 'plan'       — запрос плана/шагов
    - 'reflection' — анализ, разбор, рефлексия
    - 'coaching'   — запрос наставничества / прокачки
    - 'question'   — обычный вопрос
    - 'other'      — всё остальное
    """
    if not text:
        return "other"

    t = text.lower()

    # План / чек-лист
    plan_markers = [
        "сделай план",
        "по шагам",
        "шаг за шагом",
        "чек-лист",
        "чеклист",
        "roadmap",
        "дорожную карту",
    ]
    if any(m in t for m in plan_markers):
        return "plan"

    # Коучинг / личный рост
    coaching_markers = [
        "мотивац",
        "дисциплин",
        "привычк",
        "распорядок",
        "режим дня",
        "как мне стать",
        "хочу прокачать",
        "как научиться",
        "как перестать",
        "как начать",
    ]
    if any(m in t for m in coaching_markers):
        return "coaching"

    # Рефлексия / разбор
    reflection_markers = [
        "проанализируй",
        "анализ",
        "разбор",
        "разложи по полочкам",
        "объясни",
        "почему так",
        "что я делаю не так",
    ]
    if any(m in t for m in reflection_markers):
        return "reflection"

    # Вопрос
    question_markers = [
        "почему",
        "зачем",
        "как",
        "что",
        "когда",
        "где",
        "сколько",
        "можно ли",
        "?",
    ]
    if any(m in t for m in question_markers):
        return "question"

    return "other"


# ----------------------- Публичные функции логирования -----------------------


def log_chat_turn(
    *,
    user_id: int,
    mode_key: str,
    request_text: str,
    response_text: str,
    plan_code: Optional[str] = None,
) -> None:
    """
    Лог одного «хода» диалога:
    - тип интента
    - режим
    - длина запроса и ответа
    - тариф (опционально, можно доагрегировать по users)
    """
    try:
        intent = _detect_intent(request_text or "")
        req_len = len(request_text or "")
        resp_len = len(response_text or "")

        _insert_event(
            event_type="chat_turn",
            user_id=user_id,
            intent_type=intent,
            mode_key=mode_key,
            request_len=req_len,
            response_len=resp_len,
            plan_code=plan_code,
            extra={},
        )
    except Exception:
        logger.exception("Failed to log chat_turn metrics")


def log_limit_hit(
    *,
    user_id: int,
    plan_code: str,
    reason: str,
    daily_used: int,
    monthly_used: int,
) -> None:
    """
    Лог срабатывания лимитов (для понимания, сколько free-пользователей упираются в потолок).
    """
    try:
        _insert_event(
            event_type="limit_hit",
            user_id=user_id,
            plan_code=plan_code,
            extra={
                "reason": reason,
                "daily_used": daily_used,
                "monthly_used": monthly_used,
            },
        )
    except Exception:
        logger.exception("Failed to log limit_hit metrics")


def log_invoice_created(
    *,
    user_id: int,
    tariff_key: str,
    invoice_id: int,
    amount_usdt: float,
) -> None:
    """
    Лог создания инвойса — для воронки оплат по тарифам.
    """
    try:
        _insert_event(
            event_type="invoice_created",
            user_id=user_id,
            tariff_key=tariff_key,
            invoice_id=invoice_id,
            extra={
                "amount_usdt": amount_usdt,
            },
        )
    except Exception:
        logger.exception("Failed to log invoice_created metrics")


def log_invoice_status(
    *,
    user_id: int,
    tariff_key: str,
    invoice_id: int,
    status: str,
) -> None:
    """
    Лог проверки статуса инвойса — видно, на каких тарифах чаще всего «кидают» оплату.
    """
    try:
        _insert_event(
            event_type="invoice_status",
            user_id=user_id,
            tariff_key=tariff_key,
            invoice_id=invoice_id,
            status=status,
            extra={},
        )
    except Exception:
        logger.exception("Failed to log invoice_status metrics")
