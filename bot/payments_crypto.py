import os
from typing import Any, Dict, List

import httpx
from dotenv import load_dotenv

from .subscription_db import save_payment

load_dotenv()

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")

SUBSCRIPTION_PRICE_TON = float(os.getenv("SUBSCRIPTION_PRICE_TON", "1.0"))
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
SUBSCRIPTION_ASSET = os.getenv("SUBSCRIPTION_ASSET", "TON")


class CryptoPayError(Exception):
    pass


async def _request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN is not set")

    headers = {
        "Content-Type": "application/json",
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
    }

    async with httpx.AsyncClient(base_url=CRYPTO_PAY_API_URL, timeout=15.0) as client:
        resp = await client.post(method, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    if not data.get("ok"):
        raise CryptoPayError(str(data))

    return data["result"]


async def create_invoice(telegram_id: int) -> Dict[str, Any]:
    """
    Создаёт счёт на подписку для пользователя.
    Возвращает объект инвойса CryptoBot.
    """
    payload = {
        "asset": SUBSCRIPTION_ASSET,
        "amount": SUBSCRIPTION_PRICE_TON,
        "description": "Подписка AI Medicine Premium",
        "payload": str(telegram_id),
        "allow_anonymous": False,
        "allow_comments": False,
    }

    result = await _request("/createInvoice", payload)
    invoice_id = result["invoice_id"]
    amount = float(result.get("amount", SUBSCRIPTION_PRICE_TON))
    currency = result.get("asset", SUBSCRIPTION_ASSET)
    created_at = result.get("created_at")

    save_payment(
        invoice_id=invoice_id,
        telegram_id=telegram_id,
        amount=amount,
        currency=currency,
        status=result.get("status", "active"),
        created_at=created_at,
        payload=payload["payload"],
    )

    return result


async def fetch_invoices_statuses(invoice_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """
    Возвращает статусы указанных счетов.
    Сейчас не используется в main.py, но
    оставил на будущее, если захочешь сделать авто-проверку оплат.
    """
    if not invoice_ids:
        return {}

    payload = {
        "invoice_ids": invoice_ids,
    }
    result = await _request("/getInvoices", payload)

    statuses: Dict[int, Dict[str, Any]] = {}
    for inv in result:
        inv_id = inv["invoice_id"]
        statuses[inv_id] = inv

    return statuses
