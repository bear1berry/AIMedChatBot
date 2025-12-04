# bot/payments_crypto.py

from __future__ import annotations

import os
import time
from typing import Dict, List

import httpx
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=env_path, override=True)


class CryptoPayError(Exception):
    pass


CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_BASE_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")


def _get_headers() -> Dict[str, str]:
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN не указан в .env")
    return {
        "Content-Type": "application/json",
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
    }


async def create_invoice(
    amount: float,
    currency: str,
    description: str,
    payer_username: str | None,
) -> Dict[str, object]:
    """
    Создаёт инвойс в Crypto Pay и возвращает:
    {
        "invoice_id": int,
        "status": str,         # "active"
        "currency": str,       # "TON" / "USDT"
        "amount": float,
        "created_at": int,     # unix timestamp
        "url": str,            # bot_invoice_url
    }
    """
    headers = _get_headers()

    payload = f"user={payer_username or ''}|plan=30d|ts={int(time.time())}"

    data = {
        "asset": currency,          # "TON" или "USDT"
        "amount": float(amount),
        "description": description,
        "payload": payload,
        "allow_comments": False,
        "allow_anonymous": True,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{CRYPTO_PAY_BASE_URL}/createInvoice",
            headers=headers,
            json=data,
        )

    if resp.status_code != 200:
        raise CryptoPayError(f"createInvoice HTTP {resp.status_code}: {resp.text}")

    body = resp.json()
    if not body.get("ok"):
        raise CryptoPayError(f"createInvoice API error: {body}")

    result = body["result"]

    invoice_id = int(result["invoice_id"])
    status = str(result.get("status", "active"))
    asset = str(result.get("asset") or currency)
    amount_value = float(result.get("amount", amount))
    created_at_str = str(result.get("created_at"))
    # created_at приходит в ISO 8601, но нам достаточно текущего времени,
    # чтобы не парсить таймзоны
    created_at_ts = int(time.time())
    url = (
        result.get("bot_invoice_url")
        or result.get("mini_app_invoice_url")
        or result.get("pay_url")
    )
    if not url:
        raise CryptoPayError("В ответе Crypto Pay нет bot_invoice_url")

    return {
        "invoice_id": invoice_id,
        "status": status,
        "currency": asset,
        "amount": amount_value,
        "created_at": created_at_ts,
        "url": url,
    }


async def fetch_invoices_statuses(invoice_ids: List[int]) -> Dict[int, str]:
    """
    Получает статусы инвойсов по их ID через getInvoices.
    Возвращает словарь: invoice_id -> status ("active", "paid", "expired").
    """
    if not invoice_ids:
        return {}

    headers = _get_headers()
    data = {
        "invoice_ids": invoice_ids,
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{CRYPTO_PAY_BASE_URL}/getInvoices",
            headers=headers,
            json=data,
        )

    if resp.status_code != 200:
        raise CryptoPayError(f"getInvoices HTTP {resp.status_code}: {resp.text}")

    body = resp.json()
    if not body.get("ok"):
        raise CryptoPayError(f"getInvoices API error: {body}")

    result = body.get("result") or []
    statuses: Dict[int, str] = {}
    for inv in result:
        in
