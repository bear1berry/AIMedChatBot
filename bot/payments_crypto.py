from __future__ import annotations

import json
import os
from typing import Optional, Dict, Any, List

import httpx

from .subscription_db import create_payment, mark_payment_paid

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_BASE_URL = "https://pay.crypt.bot/api"


class CryptoPayError(Exception):
    pass


def _get_auth_headers() -> Dict[str, str]:
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN не указан в .env")
    return {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}


async def create_invoice(
    *,
    telegram_id: int,
    amount: float,
    currency: str = "TON",
    description: Optional[str] = None,
) -> str:
    """
    Создаёт инвойс в CryptoBot и сохраняет черновик платежа в БД.
    Возвращает invoice_url.
    """
    headers = _get_auth_headers()
    payload: Dict[str, Any] = {
        "amount": amount,
        "currency": currency,
    }
    if description:
        payload["description"] = description

    async with httpx.AsyncClient(base_url=CRYPTO_PAY_BASE_URL, timeout=10.0) as client:
        resp = await client.post("/createInvoice", json=payload, headers=headers)
        data = resp.json()

    if not data.get("ok"):
        raise CryptoPayError(f"Ошибка CryptoPay: {data!r}")

    result = data["result"]
    invoice_id = int(result["invoice_id"])
    invoice_url = result["pay_url"]
    raw_json = json.dumps(result, ensure_ascii=False)

    # Создаём запись о платеже в статусе "pending"
    create_payment(
        telegram_id=telegram_id,
        invoice_id=invoice_id,
        currency=currency,
        amount=amount,
        status="pending",
        created_at=int(result.get("created_at", 0)) or 0,
        paid_at=None,
        raw_json=raw_json,
    )

    return invoice_url


async def fetch_invoices(
    *,
    invoice_ids: Optional[List[int]] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Вспомогательный метод для /admin, чтобы подтянуть реальные статусы инвойсов.
    """
    headers = _get_auth_headers()
    payload: Dict[str, Any] = {}
    if invoice_ids:
        payload["invoice_ids"] = ",".join(str(i) for i in invoice_ids)
    if status:
        payload["status"] = status

    async with httpx.AsyncClient(base_url=CRYPTO_PAY_BASE_URL, timeout=10.0) as client:
        resp = await client.get("/getInvoices", params=payload, headers=headers)
        data = resp.json()

    if not data.get("ok"):
        raise CryptoPayError(f"Ошибка CryptoPay (getInvoices): {data!r}")

    return data["result"]


async def sync_paid_invoices_from_cryptobot(invoice_ids: List[int]) -> None:
    """
    Пример поллинга: берём список invoice_id из БД,
    спрашиваем у CryptoPay их статус, все 'paid' помечаем оплаченными.
    """
    if not invoice_ids:
        return

    result = await fetch_invoices(invoice_ids=invoice_ids)
    # result — список инвойсов
    for inv in result:
        if inv.get("status") == "paid":
            inv_id = int(inv["invoice_id"])
            paid_at = int(inv.get("paid_at", 0)) or None
            mark_payment_paid(inv_id, paid_at=paid_at)
