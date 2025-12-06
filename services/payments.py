from __future__ import annotations

import logging
from typing import Dict, Any, Optional

import httpx

from bot.config import CRYPTO_PAY_API_URL, CRYPTO_PAY_API_TOKEN, SUBSCRIPTION_TARIFFS

logger = logging.getLogger(__name__)


async def _cryptopay_request(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not CRYPTO_PAY_API_TOKEN:
        raise RuntimeError("CRYPTO_PAY_API_TOKEN is not configured")

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
    }

    url = CRYPTO_PAY_API_URL.rstrip("/") + f"/{method}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"CryptoPay API error: {data}")
        return data["result"]


async def create_cryptobot_invoice(tariff_key: str) -> Optional[Dict[str, Any]]:
    """
    Создать счёт в CryptoBot для выбранного тарифа.
    Возвращает dict с полями invoice_id, bot_invoice_url, amount, status и т.д.
    """
    tariff = SUBSCRIPTION_TARIFFS.get(tariff_key)
    if not tariff:
        raise ValueError(f"Unknown tariff: {tariff_key}")

    payload = {
        "asset": "USDT",
        "amount": tariff["price_usdt"],
        "description": tariff["title"],
        "payload": tariff["code"],
        "allow_comments": False,
        "allow_anonymous": True,
    }

    try:
        result = await _cryptopay_request("createInvoice", payload)
        return result
    except Exception as e:
        logger.exception("Failed to create CryptoBot invoice: %s", e)
        return None


async def get_invoice_status(invoice_id: int) -> Optional[str]:
    """
    Получить статус счёта по его ID.
    Возвращает строку статуса (active/paid/cancelled/expired) или None.
    """
    payload = {
        "invoice_ids": [invoice_id],
    }
    try:
        result = await _cryptopay_request("getInvoices", payload)
        if not result:
            return None
        invoice = result[0]
        return invoice.get("status")
    except Exception as e:
        logger.exception("Failed to get CryptoBot invoice status: %s", e)
        return None
