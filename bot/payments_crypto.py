from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import httpx


class CryptoPayError(Exception):
    pass


CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_BASE_URL = "https://pay.crypt.bot/api"


async def _request(
    method: str,
    endpoint: str,
    *,
    json: Optional[dict] = None,
    params: Optional[dict] = None,
) -> dict:
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN не указан в .env")

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
        "Content-Type": "application/json",
    }

    url = f"{CRYPTO_PAY_BASE_URL}{endpoint}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(method.upper(), url, headers=headers, json=json, params=params)

    try:
        data = resp.json()
    except Exception as e:
        raise CryptoPayError(f"Некорректный ответ от CryptoPay: {resp.text}") from e

    if not data.get("ok"):
        description = data.get("description") or "unknown error"
        raise CryptoPayError(f"CryptoPay error: {description}")

    return data["result"]


async def create_invoice(
    *,
    telegram_id: int,
    username: Optional[str],
    amount: int,
    description: str,
    asset: str = "TON",
) -> Tuple[str, str]:
    """
    Создать счёт в CryptoPay.

    Возвращает (invoice_id, pay_url).
    """
    payload = {
        "asset": asset,
        "amount": str(amount),
        "description": description,
        "payload": str(telegram_id),
        "hidden_message": f"Подписка для @{username}" if username else "Подписка",
        "allow_anonymous": False,
        "expires_in": 900,
    }

    result = await _request("POST", "/createInvoice", json=payload)

    invoice_id = str(result.get("invoice_id"))
    pay_url = result.get("pay_url") or result.get("bot_invoice_url")
    if not invoice_id or not pay_url:
        raise CryptoPayError("Не удалось получить invoice_id/pay_url от CryptoPay")

    return invoice_id, pay_url


async def fetch_invoices_statuses(invoice_ids: List[str]) -> Dict[str, str]:
    """
    Получить статусы счетов по их invoice_id.

    Возвращает dict {invoice_id: status}.
    """
    if not invoice_ids:
        return {}

    params = {
        "invoice_ids": ",".join(invoice_ids),
    }
    result = await _request("GET", "/getInvoices", params=params)

    statuses: Dict[str, str] = {}
    invoices = result if isinstance(result, list) else result.get("items", [])
    for inv in invoices:
        inv_id = str(inv.get("invoice_id"))
        status = inv.get("status")
        if inv_id:
            statuses[inv_id] = status
    return statuses
