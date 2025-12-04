# bot/payments_crypto.py

from __future__ import annotations

import logging
import os
from typing import Union

import httpx

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")


class CryptoPayError(Exception):
    """Ошибка при работе с Crypto Pay API."""


async def crypto_pay_get_me() -> None:
    """
    Одноразовая проверка токена при старте (можно вызвать из main, не обязательно).
    Пишет результат в лог.
    """
    if not CRYPTO_PAY_API_TOKEN:
        logging.warning("Crypto Pay: CRYPTO_PAY_API_TOKEN не задан, платежи отключены")
        return

    url = f"{CRYPTO_PAY_API_URL.rstrip('/')}/getMe"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, headers=headers)

    try:
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        logging.exception("Crypto Pay getMe: невалидный JSON: %s", resp.text)
        return

    if body.get("ok"):
        logging.info("Crypto Pay getMe OK: %s", body.get("result"))
    else:
        logging.error("Crypto Pay getMe ERROR: %s", body.get("error"))


async def create_invoice(
    *,
    asset: str,
    amount: Union[float, str],
    description: str,
    payload: str,
) -> str:
    """
    Создаёт инвойс через Crypto Pay и возвращает ссылку на оплату.

    Документация: https://help.crypt.bot/crypto-pay-api#createInvoice
    """

    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN не указан в .env")

    # Crypto Pay по спекам хочет amount как строку с float
    if isinstance(amount, float):
        amount_str = f"{amount:.8f}".rstrip("0").rstrip(".")
    else:
        amount_str = str(amount)

    url = f"{CRYPTO_PAY_API_URL.rstrip('/')}/createInvoice"
    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
        "Content-Type": "application/json",
    }
    data = {
        "currency_type": "crypto",
        "asset": asset,
        "amount": amount_str,
        "description": description,
        "payload": payload,
        "allow_anonymous": True,
        "allow_comments": True,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, headers=headers, json=data)

    try:
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        logging.exception(
            "Crypto Pay createInvoice: не удалось распарсить JSON: %s", resp.text
        )
        raise CryptoPayError(f"Неверный ответ Crypto Pay: {e}") from e

    if not body.get("ok"):
        error = body.get("error") or {}
        name = error.get("name") or "UNKNOWN_ERROR"
        message = error.get("message") or str(body)
        logging.error(
            "Crypto Pay createInvoice ERROR: name=%s message=%s", name, message
        )
        raise CryptoPayError(f"{name}: {message}")

    result = body.get("result") or {}
    # по новым спецификациям основной URL — bot_invoice_url
    invoice_url = result.get("bot_invoice_url") or result.get("pay_url")
    if not invoice_url:
        logging.error(
            "Crypto Pay createInvoice: нет ссылки на оплату в ответе: %s", body
        )
        raise CryptoPayError("Crypto Pay не вернул ссылку на оплату")

    logging.info("Crypto Pay invoice создан: %s", result)
    return invoice_url
