"""
Crypto payments integration for BlackBoxGPT bot.

Uses Crypto Pay API (@CryptoBot) to create and check invoices.
Docs: https://help.crypt.bot/crypto-pay-api
"""

from __future__ import annotations

import os
import logging
from typing import Any, Dict, Optional, Tuple

import httpx

# === Config ===

CRYPTO_PAY_API_TOKEN = (
    os.getenv("CRYPTO_PAY_API_TOKEN")
    or os.getenv("CRYPTO_BOT_API_KEY")
    or os.getenv("CRYPTO_BOT_TOKEN")
    or os.getenv("CRYPTO_PAY_TOKEN")
)

CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot")

# Какой ассет использовать (по умолчанию USDT).
# Если хочешь TON — просто поставь CRYPTO_PAY_ASSET=TON в .env
CRYPTO_PAY_ASSET = os.getenv("CRYPTO_PAY_ASSET") or os.getenv("CRYPTO_ASSET") or "USDT"

# Тарифы подписки.
# Ключи — код плана, который используется в main.py в callback_data.
# Значения: (amount, period_days)
_PLAN_VARIANTS: Dict[str, Tuple[str, int]] = {
    # 1 месяц
    "month": ("5", 30),
    "1m": ("5", 30),
    "plan_1m": ("5", 30),
    # 3 месяца
    "3m": ("12", 90),
    "3months": ("12", 90),
    "plan_3m": ("12", 90),
    # 12 месяцев
    "year": ("60", 365),
    "12m": ("60", 365),
    "plan_12m": ("60", 365),
}

# Если код плана не распознан — используем этот
DEFAULT_PLAN_CODE = "month"


class CryptoPayError(RuntimeError):
    pass


def _ensure_config() -> None:
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError(
            "CRYPTO_PAY_API_TOKEN / CRYPTO_BOT_API_KEY / CRYPTO_BOT_TOKEN / CRYPTO_PAY_TOKEN "
            "не задан в .env (нужен для крипто-оплат)."
        )


async def _call_crypto_pay(
    method: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Низкоуровневый helper для Crypto Pay API.
    """
    _ensure_config()

    url = f"{CRYPTO_PAY_API_URL}/api/{method}"

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        if json is not None:
            resp = await client.post(url, headers=headers, json=json)
        else:
            resp = await client.get(url, headers=headers, params=params)

    data = resp.json()
    if not data.get("ok"):
        logging.error("Crypto Pay API error in %s: %s", method, data)
        raise CryptoPayError(f"Crypto Pay API error in {method}: {data}")
    return data["result"]


def _resolve_plan(plan_code: Optional[str]) -> Tuple[str, int, str]:
    """
    Возвращает: (amount, period_days, normalized_code)
    """
    if not plan_code:
        plan_code = DEFAULT_PLAN_CODE

    plan_code_lower = plan_code.lower()
    if plan_code_lower not in _PLAN_VARIANTS:
        # Если прилетел неизвестный код — откатываемся к дефолту
        plan_code_lower = DEFAULT_PLAN_CODE

    amount, period_days = _PLAN_VARIANTS[plan_code_lower]

    # Нормализуем имя плана, чтобы везде было единообразно
    if period_days == 30:
        normalized = "month"
    elif period_days == 90:
        normalized = "3m"
    else:
        normalized = "12m"

    return amount, period_days, normalized


async def create_invoice(telegram_id: int, plan_code: Optional[str] = None) -> Dict[str, Any]:
    """
    Создать инвойс в Crypto Pay и вернуть структуру:

    {
        "invoice_id": int,
        "pay_url": str,
        "amount": str,
        "asset": str,
        "period_days": int,
        "plan_code": str,
    }
    """
    amount, period_days, normalized_code = _resolve_plan(plan_code)

    description = f"Премиум-доступ: {normalized_code} (BlackBox GPT)."

    payload: Dict[str, Any] = {
        "asset": CRYPTO_PAY_ASSET,
        "amount": amount,
        "description": description,
        # В payload зашьём id пользователя и план — пригодится, если будешь использовать вебхуки
        "payload": f"user={telegram_id};plan={normalized_code}",
    }

    result = await _call_crypto_pay("createInvoice", json=payload)

    # result — это словарь с полями инвойса (см. доку Crypto Pay).
    invoice_id = result.get("invoice_id") or result.get("id")
    pay_url = result["pay_url"]

    return {
        "invoice_id": invoice_id,
        "pay_url": pay_url,
        "amount": amount,
        "asset": CRYPTO_PAY_ASSET,
        "period_days": period_days,
        "plan_code": normalized_code,
    }


async def fetch_invoice_status(invoice_id: int | str) -> Optional[Dict[str, Any]]:
    """
    Получить статус инвойса из Crypto Pay по ID.

    Возвращает dict:

    {
        "invoice_id": int,
        "status": str,
        "amount": str,
        "asset": str,
        "hash": str | None,
        "created_at": str,
        "paid_at": str | None,
        "description": str | None,
        "payload": str | None,
    }

    или None, если не найден или ошибка.
    """
    try:
        # Crypto Pay ожидает invoice_ids строкой через запятую
        result = await _call_crypto_pay(
            "getInvoices",
            params={"invoice_ids": str(invoice_id)},
        )
    except Exception as e:
        logging.exception("Error fetching invoice %s from Crypto Pay: %s", invoice_id, e)
        return None

    if not result:
        logging.warning("Invoice %s not found in Crypto Pay", invoice_id)
        return None

    inv = result[0]
    return {
        "invoice_id": inv.get("invoice_id") or inv.get("id"),
        "status": inv.get("status"),
        "amount": inv.get("amount"),
        "asset": inv.get("asset"),
        "hash": inv.get("hash"),
        "created_at": inv.get("created_at"),
        "paid_at": inv.get("paid_at"),
        "description": inv.get("description"),
        "payload": inv.get("payload"),
    }
