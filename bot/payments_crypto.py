from __future__ import annotations

import json
import os
from typing import Optional, Dict, Any, List

import httpx

from .subscription_db import create_payment, mark_payment_paid

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_BASE_URL = "https://pay.crypt.bot/api"


class CryptoPayError(Exception):
    """Ошибка при работе с Crypto Bot API."""
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
    Создаёт инвойс в Crypto Bot и сохраняет его в БД как 'pending'.
    Возвращает URL для оплаты.
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
        raise CryptoPayError(f"Ошибка CryptoPay (createInvoice): {data!r}")

    result = data["result"]
    invoice_id = int(result["invoice_id"])
    invoice_url = result["pay_url"]
    created_at = int(result.get("created_at", 0)) or 0
    raw_json = json.dumps(result, ensure_ascii=False)

    # Сохраняем платёж в статусе pending
    create_payment(
        telegram_id=telegram_id,
        invoice_id=invoice_id,
        currency=currency,
        amount=amount,
        status="pending",
        created_at=created_at,
        paid_at=None,
        raw_json=raw_json,
    )

    return invoice_url


async def fetch_invoices(
    *,
    invoice_ids: Optional[List[int]] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Базовый вызов getInvoices.
    Возвращает список инвойсов так, как его отдаёт Crypto Bot.
    """
    headers = _get_auth_headers()
    params: Dict[str, Any] = {}
    if invoice_ids:
        params["invoice_ids"] = ",".join(str(i) for i in invoice_ids)
    if status:
        params["status"] = status

    async with httpx.AsyncClient(base_url=CRYPTO_PAY_BASE_URL, timeout=10.0) as client:
        resp = await client.get("/getInvoices", params=params, headers=headers)
        data = resp.json()

    if not data.get("ok"):
        raise CryptoPayError(f"Ошибка CryptoPay (getInvoices): {data!r}")

    return data["result"]


async def fetch_invoices_statuses(invoice_ids: List[int]) -> Dict[int, str]:
    """
    Функция, которую ждёт main.py:
    принимает список invoice_id и возвращает словарь {invoice_id: status}.
    """
    if not invoice_ids:
        return {}

    invoices = await fetch_invoices(invoice_ids=invoice_ids)
    result: Dict[int, str] = {}
    for inv in invoices:
        try:
            inv_id = int(inv["invoice_id"])
            status = str(inv.get("status", ""))
            result[inv_id] = status
        except (KeyError, ValueError, TypeError):
            continue
    return result


async def sync_paid_invoices_from_cryptobot(invoice_ids: List[int]) -> None:
    """
    Утилита для /admin: подтягиваем реальные статусы по списку инвойсов
    и помечаем оплаченные как paid (с выдачей подписки).
    """
    if not invoice_ids:
        return

    invoices = await fetch_invoices(invoice_ids=invoice_ids)
    for inv in invoices:
        if str(inv.get("status")) == "paid":
            try:
                inv_id = int(inv["invoice_id"])
            except (KeyError, ValueError, TypeError):
                continue

            paid_at = inv.get("paid_at")
            try:
                paid_ts = int(paid_at) if paid_at is not None else N_
