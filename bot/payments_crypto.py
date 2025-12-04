from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_BASE_URL = "https://pay.crypt.bot/api"


class CryptoPayError(Exception):
    pass


def _require_token() -> str:
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError(
            "CRYPTO_PAY_API_TOKEN не указан в .env, оплата через CryptoBot недоступна"
        )
    return CRYPTO_PAY_API_TOKEN


async def _call_api(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    token = _require_token()
    url = f"{CRYPTO_PAY_BASE_URL}/{method}"
    headers = {"Crypto-Pay-API-Token": token}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, headers=headers)
    try:
        data = resp.json()
    except Exception as e:  # защитный код
        logger.exception("CryptoPay: invalid JSON response: %s", resp.text)
        raise CryptoPayError(f"Некорректный ответ CryptoPay: {e}") from e

    if not data.get("ok"):
        logger.error("CryptoPay API error: %s", data)
        raise CryptoPayError(str(data.get("error", "Unknown CryptoPay error")))
    return data["result"]


async def create_invoice(
    asset: str,
    amount: float,
    description: str,
    payload: str | None = None,
) -> Dict[str, Any]:
    """
    Create CryptoBot invoice.
    Returns full invoice dict from API.
    """
    req: Dict[str, Any] = {
        "asset": asset,
        "amount": amount,
        "description": description,
        "allow_comments": False,
        "allow_anonymous": True,
    }
    if payload:
        req["payload"] = payload

    result = await _call_api("createInvoice", req)
    # API returns dict with 'invoice_id', 'status', 'pay_url', etc.
    return result


async def get_invoice(invoice_id: str) -> Dict[str, Any]:
    """
    Fetch single invoice by id.
    """
    result = await _call_api("getInvoices", {"invoice_ids": [invoice_id]})
    invoices = result.get("items") or result.get("invoices") or []
    if not invoices:
        raise CryptoPayError(f"Invoice {invoice_id} not found")
    return invoices[0]


async def fetch_invoices_statuses(invoice_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Backwards-compatible helper: return mapping invoice_id -> invoice dict.
    (на случай, если где-то ещё импортируется)
    """
    if not invoice_ids:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for inv_id in invoice_ids:
        try:
            inv = await get_invoice(inv_id)
        except CryptoPayError as e:
            logger.error("Failed to fetch invoice %s: %s", inv_id, e)
            continue
        out[inv_id] = inv
    return out
