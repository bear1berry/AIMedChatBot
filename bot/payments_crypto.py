import os

from dotenv import load_dotenv
from typing import Optional

import httpx

from .subscription_db import save_payment

load_dotenv()

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")

# 5 TON / USDT за 30 дней
PLAN_AMOUNT_TON = "5"
PLAN_AMOUNT_USDT = "5"
PLAN_TITLE = "Подписка на 30 дней — Premium доступ к боту"


class CryptoPayError(Exception):
    pass


async def _api_request(method: str, payload: Optional[dict] = None) -> dict:
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN не указан в .env")

    if payload is None:
        payload = {}

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{CRYPTO_PAY_API_URL}/{method}",
            json=payload,
            headers=headers,
        )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise CryptoPayError(str(data))
    return data["result"]


async def create_invoice(user_id: int, plan_code: str) -> str:
    """
    Создаёт счёт на оплату через Crypto Bot и возвращает ссылку.
    Пока создаём один инвойс в TON.
    """
    ton_invoice = await _api_request(
        "createInvoice",
        {
            "asset": "TON",
            "amount": PLAN_AMOUNT_TON,
            "description": PLAN_TITLE,
            "payload": f"{plan_code}:{user_id}",
        },
    )

    # фиксируем платёж как ожидающий
    save_payment(
        invoice_id=ton_invoice["invoice_id"],
        telegram_id=user_id,
        amount=float(PLAN_AMOUNT_TON),
        currency="TON",
        status=ton_invoice["status"],
        created_at=ton_invoice.get("created_at"),
    )

    return ton_invoice["pay_url"]
