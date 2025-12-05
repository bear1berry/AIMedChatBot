import logging
from typing import Tuple, Optional

import httpx

from bot.config import CRYPTO_PAY_API_URL, CRYPTO_PAY_API_TOKEN, SUBSCRIPTION_TARIFFS

log = logging.getLogger(__name__)

ASSET = "USDT"  # работаем в USDT, как ты просил


async def create_cryptobot_invoice(user_id: int, tariff_key: str) -> Tuple[int, str]:
    """
    Создаёт инвойс в CryptoBot (Crypto Pay API) и возвращает (invoice_id, pay_url).
    """
    if not CRYPTO_PAY_API_TOKEN:
        raise RuntimeError("CRYPTO_PAY_API_TOKEN is not configured")

    tariff = SUBSCRIPTION_TARIFFS.get(tariff_key)
    if not tariff:
        raise ValueError(f"Unknown tariff key: {tariff_key}")

    amount = float(tariff["amount"])
    amount_str = f"{amount:.2f}"
    description = f"BlackBoxGPT — {tariff['title']}"
    payload_str = f"user_id={user_id};tariff={tariff_key};plan=premium"

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
        "Content-Type": "application/json",
    }
    json_payload = {
        "asset": ASSET,
        "amount": amount_str,
        "description": description,
        "payload": payload_str,
        "allow_comments": False,
        "allow_anonymous": False,
    }

    url = CRYPTO_PAY_API_URL.rstrip("/") + "/createInvoice"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, headers=headers, json=json_payload)
        resp.raise_for_status()
        data = resp.json()

    if not data.get("ok"):
        log.error("CryptoBot createInvoice error: %s", data)
        raise RuntimeError("Failed to create invoice in CryptoBot")

    result = data.get("result") or {}
    invoice_id = int(result.get("invoice_id"))
    pay_url = result.get("pay_url") or result.get("bot_invoice_url")
    if not invoice_id or not pay_url:
        raise RuntimeError(f"Unexpected CryptoBot response: {data}")

    return invoice_id, pay_url


async def get_invoice_status(invoice_id: int) -> Optional[str]:
    """
    Возвращает статус инвойса в CryptoBot: active / paid / expired / cancelled / ...
    """
    if not CRYPTO_PAY_API_TOKEN:
        raise RuntimeError("CRYPTO_PAY_API_TOKEN is not configured")

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
    }
    url = CRYPTO_PAY_API_URL.rstrip("/") + "/getInvoices"
    params = {
        "invoice_ids": invoice_id,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    if not data.get("ok"):
        log.error("CryptoBot getInvoices error: %s", data)
        raise RuntimeError("Failed to fetch invoice from CryptoBot")

    result = data.get("result") or {}
    items = result.get("items") or []
    if not items:
        return None

    inv = items[0]
    return inv.get("status")
