from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")

ADMIN_USERNAMES = {
    u.strip().lower()
    for u in os.getenv("ADMIN_USERNAMES", "").replace(",", " ").split()
    if u.strip()
}

FREE_MESSAGES_LIMIT = int(os.getenv("FREE_MESSAGES_LIMIT", "20"))

DB_PATH = os.getenv("SUBSCRIPTION_DB_PATH", "subscription.db")


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in .env")

LLM_AVAILABLE = bool(DEEPSEEK_API_KEY or GROQ_API_KEY)


# ---------------------------------------------------------------------------
# Database helpers (v2 schema)
# ---------------------------------------------------------------------------


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаём таблицы, если их ещё нет (v2-схема)."""
    with _get_conn() as conn:
        cur = conn.cursor()

        # Users table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users_v2 (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id       INTEGER UNIQUE NOT NULL,
                username          TEXT,
                first_name        TEXT,
                last_name         TEXT,
                is_premium        INTEGER NOT NULL DEFAULT 0,
                premium_until_ts  INTEGER,
                free_used         INTEGER NOT NULL DEFAULT 0,
                created_at_ts     INTEGER NOT NULL,
                updated_at_ts     INTEGER NOT NULL
            )
            """
        )

        # Invoices table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS invoices_v2 (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id       TEXT UNIQUE NOT NULL,
                telegram_id      INTEGER NOT NULL,
                plan_code        TEXT NOT NULL,
                asset            TEXT NOT NULL,
                amount           REAL NOT NULL,
                status           TEXT NOT NULL,
                created_at_ts    INTEGER NOT NULL,
                paid_at_ts       INTEGER,
                raw_json         TEXT
            )
            """
        )

        # Projects table
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS projects_v2 (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                title         TEXT NOT NULL,
                description   TEXT NOT NULL,
                updated_at_ts INTEGER NOT NULL
            )
            """
        )

        conn.commit()


def get_or_create_user_record(
    telegram_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
) -> sqlite3.Row:
    now = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                """
                UPDATE users_v2
                SET username = ?, first_name = ?, last_name = ?, updated_at_ts = ?
                WHERE telegram_id = ?
                """,
                (username, first_name, last_name, now, telegram_id),
            )
        else:
            cur.execute(
                """
                INSERT INTO users_v2 (
                    telegram_id, username, first_name, last_name,
                    is_premium, premium_until_ts, free_used,
                    created_at_ts, updated_at_ts
                ) VALUES (?, ?, ?, ?, 0, NULL, 0, ?, ?)
                """,
                (telegram_id, username, first_name, last_name, now, now),
            )
        conn.commit()
        cur.execute(
            "SELECT * FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        return cur.fetchone()


def is_user_admin(username: Optional[str]) -> bool:
    return bool(username and username.lower() in ADMIN_USERNAMES)


def user_is_premium(row: sqlite3.Row) -> bool:
    ts = row["premium_until_ts"]
    return bool(ts and ts > int(time.time()))


def grant_premium(telegram_id: int, months: int) -> None:
    """Продлить / выдать премиум на N месяцев."""
    seconds = int(months * 30.4375 * 24 * 3600)  # ~месяцы
    now = int(time.time())
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT premium_until_ts FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        current = row["premium_until_ts"] if row else None
        base = current if current and current > now else now
        new_until = base + seconds
        cur.execute(
            """
            UPDATE users_v2
            SET is_premium = 1, premium_until_ts = ?, updated_at_ts = ?
            WHERE telegram_id = ?
            """,
            (new_until, now, telegram_id),
        )
        conn.commit()


def get_free_used(telegram_id: int) -> int:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT free_used FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        return int(row["free_used"]) if row else 0


def increment_free_used(telegram_id: int, delta: int = 1) -> int:
    with _get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users_v2 SET free_used = free_used + ? WHERE telegram_id = ?",
            (delta, telegram_id),
        )
        conn.commit()
        cur.execute(
            "SELECT free_used FROM users_v2 WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        return int(row["free_used"]) if row else delta


def create_invoice_record(
    invoice_id: str,
    telegram_id: int,
    plan_code: str,
    asset: str,
    amount: float,
    status: str,
   
