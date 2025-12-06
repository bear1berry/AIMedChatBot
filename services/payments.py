from __future__ import annotations

import logging
from typing import Optional, Tuple, Dict, Any

import httpx

from bot.config import CRYPTO_PAY_API_URL, CRYPTO_PAY_API_TOKEN, SUBSCRIPTION_TARIFFS

log = logging.getLogger(__name__)


def _headers() -> Dict[str, str]:
    return {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
        "Content-Type": "application/json",
    }


async def create_cryptobot_invoice(tariff_code: str, user_id: int) -> Optional[Dict[str, Any]]:
    """
    Создаёт инвойс в Crypto Bot и возвращает (invoice_id, invoice_url).

    tariff_code: один из ключей SUBSCRIPTION_TARIFFS: "month" / "quarter" / "year".
    """
    if not CRYPTO_PAY_API_TOKEN:
        log.warning("CRYPTO_PAY_API_TOKEN is not set; crypto payments disabled.")
        return None

    tariff = SUBSCRIPTION_TARIFFS.get(tariff_code)
    if not tariff:
        log.error("Unknown tariff_code %r", tariff_code)
        return None

    payload: Dict[str, Any] = {
        "asset": tariff["asset"],            # например, "USDT"
        "amount": str(tariff["price_usdt"]), # строкой
        "description": tariff["title"],
        "payload": str(user_id),
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{CRYPTO_PAY_API_URL.rstrip('/')}/createInvoice",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.error("Crypto Pay createInvoice error: %s", data)
            return None
        invoice = data.get("result") or {}
        invoice_id = invoice.get("invoice_id")
        url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
        if invoice_id is None or not url:
            log.error("Crypto Pay createInvoice missing fields: %s", invoice)
            return None
        return {"id": int(invoice_id), "url": str(url)}


async def get_invoice_status(invoice_id: int) -> Optional[str]:
    """
    Возвращает статус инвойса: 'active' | 'paid' | 'expired' или None при ошибке.
    """
    if not CRYPTO_PAY_API_TOKEN:
        log.warning("CRYPTO_PAY_API_TOKEN is not set; crypto payments disabled.")
        return None

    params = {
        "invoice_ids": str(invoice_id),
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(
            f"{CRYPTO_PAY_API_URL.rstrip('/')}/getInvoices",
            params=params,
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.error("Crypto Pay getInvoices error: %s", data)
            return None
