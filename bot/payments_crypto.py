# bot/payments_crypto.py
from __future__ import annotations

import os
import logging
from typing import Dict, List

import httpx

logger = logging.getLogger(__name__)

API_BASE_URL = "https://pay.crypt.bot/api"


class CryptoPayError(Exception):
    """Ошибка при работе с Crypto Bot API."""
    pass


def _get_api_token() -> str:
    token = os.getenv("CRYPTO_PAY_API_TOKEN")
    if not token:
        # В логах у тебя как раз такая ошибка и была — оставляем её же
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN не указан в .env")
    return token


async def create_invoice(
    amount: float,
    currency: str,
    description: str,
    payload: str,
) -> tuple[str, str]:
    """
    Создать счёт в Crypto Bot.

    Возвращает:
        (pay_url, invoice_id)
    """
    token = _get_api_token()
    headers = {"Crypto-Pay-API-Token": token}
    json_data = {
        "amount": amount,
        "asset": currency,       # USDT, TON, BTC и т.п.
        "description": description,
        "payload": payload,      # строка, которую ты потом можешь использовать
    }

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=20.0) as client:
        resp = await client.post("/createInvoice", headers=headers, json=json_data)

    try:
        data = resp.json()
    except Exception as exc:
        logger.exception("CryptoPay invalid JSON on createInvoice: %s", resp.text)
        raise CryptoPayError("Некорректный ответ от Crypto Bot") from exc

    if not data.get("ok"):
        logger.error("CryptoPay error response on createInvoice: %s", data)
        raise CryptoPayError("Не удалось создать счёт через Crypto Bot")

    result = data.get("result") or data.get("result", {})

    # В одних версиях API result — это объект с полями pay_url / invoice_id
    if isinstance(result, dict) and "pay_url" in result:
        pay_url = result["pay_url"]
        invoice_id = str(result.get("invoice_id") or result.get("invoiceId"))
        return pay_url, invoice_id

    # В других — result = {"items": [ {...}, ... ]}
    items = result.get("items") if isinstance(result, dict) else None
    if items:
        item = items[0]
        pay_url = item["pay_url"]
        invoice_id = str(item.get("invoice_id") or item.get("invoiceId"))
        return pay_url, invoice_id

    logger.error("CryptoPay unexpected result structure on createInvoice: %s", data)
    raise CryptoPayError("Не удалось разобрать ответ от Crypto Bot при создании счёта")


async def fetch_invoices_statuses(invoice_ids: List[str]) -> Dict[str, str]:
    """
    Получить статусы нескольких инвойсов.

    Возвращает словарь:
        {invoice_id: status}

    Используется в main.py / админ-панели для массовой проверки и активации подписок.
    """
    if not invoice_ids:
        return {}

    token = _get_api_token()
    headers = {"Crypto-Pay-API-Token": token}
    params = {"invoice_ids": ",".join(map(str, invoice_ids))}

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=20.0) as client:
        resp = await client.get("/getInvoices", headers=headers, params=params)

    try:
        data = resp.json()
    except Exception as exc:
        logger.exception("CryptoPay invalid JSON on getInvoices: %s", resp.text)
        raise CryptoPayError("Некорректный ответ от Crypto Bot при получении статусов") from exc

    if not data.get("ok"):
        logger.error("CryptoPay error response on getInvoices: %s", data)
        raise CryptoPayError("Не удалось получить статусы счетов Crypto Bot")

    result = data.get("result") or {}
    items = result.get("items") or []

    statuses: Dict[str, str] = {}
    for item in items:
        inv_id = str(item.get("invoice_id") or item.get("invoiceId"))
        status = item.get("status", "")
        if inv_id:
            statuses[inv_id] = status

    return statuses
