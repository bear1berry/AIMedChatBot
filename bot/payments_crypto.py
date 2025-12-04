# bot/payments_crypto.py

import os
from typing import Literal, Optional, Dict, Any

import httpx  # убедись, что httpx есть в requirements.txt

from .subscription_db import create_payment

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")


class CryptoPayError(Exception):
    pass


async def create_invoice(
    *,
    telegram_id: int,
    plan_code: str,
    asset: Literal["TON", "USDT"],
    amount: float,
    description: str,
) -> Dict[str, Any]:
    """
    Создаём invoice в Crypto Pay и сохраняем его в БД.

    Возвращаем dict с полями:
    {
        "invoice_id": str,
        "pay_url": str
    }
    """
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN не задан в переменных окружения")

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {
        "asset": asset,
        "amount": float(amount),
        "description": description,
        "payload": f"user:{telegram_id}|plan:{plan_code}",
        "allow_comments": False,
        "allow_anonymous": True,
        "expires_in": 3600,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{CRYPTO_PAY_API_URL}/createInvoice",
            headers=headers,
            json=payload,
        )

    data = resp.json()
    if not data.get("ok"):
        raise CryptoPayError(f"Ошибка Crypto Pay: {data!r}")

    invoice = data["result"]
    invoice_id = str(invoice["invoice_id"])
    pay_url = invoice.get("pay_url") or invoice.get("bot_invoice_url")

    create_payment(
        invoice_id=invoice_id,
        telegram_id=telegram_id,
        plan_code=plan_code,
        asset=asset,
        amount=float(amount),
    )

    return {
        "invoice_id": invoice_id,
        "pay_url": pay_url,
    }


async def get_invoice_status(invoice_id: str) -> Optional[str]:
    """
    Возвращаем статус инвойса: active / paid / cancelled / expired и т.д.
    """
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN не задан в переменных окружения")

    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}
    params = {"invoice_ids": invoice_id}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{CRYPTO_PAY_API_URL}/getInvoices",
            headers=headers,
            params=params,
        )

    data = resp.json()
    if not data.get("ok"):
        raise CryptoPayError(f"Ошибка Crypto Pay (getInvoices): {data!r}")

    result = data.get("result")
    items = []
    if isinstance(result, dict) and "items" in result:
        items = result["items"]
    elif isinstance(result, list):
        items = result

    if not items:
        return None

    invoice = items[0]
    return invoice.get("status")
