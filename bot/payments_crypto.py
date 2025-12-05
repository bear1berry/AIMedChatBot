from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterable, Mapping

import httpx

logger = logging.getLogger(__name__)

# === Конфиг из .env ===

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN") or ""
CRYPTO_PAY_API_URL = (os.getenv("CRYPTO_PAY_API_URL") or "https://pay.crypt.bot/api").rstrip("/")

# Можно переопределить в .env переменной CRYPTO_PAY_ASSET=USDT
CRYPTO_PAY_ASSET = (os.getenv("CRYPTO_PAY_ASSET") or "TON").strip().upper() or "TON"

# Тарифы: код плана -> кол-во месяцев
PLAN_MONTHS: Dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "12m": 12,
}

# Тарифы: код плана -> цена (USDT/TON)
PLAN_PRICES: Dict[str, float] = {
    "1m": 5.0,   # 1 месяц
    "3m": 12.0,  # 3 месяца
    "12m": 60.0, # 12 месяцев
}


class CryptoPayError(RuntimeError):
    """Высокоуровневая ошибка для проблем с Crypto Bot API."""


# === Низкоуровневый вызов API ===

async def _crypto_post(method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """
    Вызов метода Crypto Bot API.

    :param method: имя метода, например 'createInvoice'
    :param payload: JSON-параметры запроса
    :return: поле "result" из ответа
    """
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN не задан в окружении (.env)")

    url = f"{CRYPTO_PAY_API_URL}/{method.lstrip('/')}"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN}

    logger.info("CRYPTOBOT: POST %s payload=%s", url, payload)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload, headers=headers)

    logger.info("CRYPTOBOT: status=%s body=%s", resp.status_code, resp.text)

    if resp.status_code != 200:
        raise CryptoPayError(f"CryptoBot HTTP {resp.status_code}: {resp.text}")

    data = resp.json()
    if not data.get("ok"):
        raise CryptoPayError(f"CryptoBot error: {data}")

    # data = {"ok": true, "result": {...} или [...]}
    return data["result"]


# === Высокоуровневые функции для бота ===

async def create_invoice(
    telegram_id: int,
    plan_code: str = "1m",
    *,
    amount: float | None = None,
    description: str | None = None,
    invoice_payload: str | None = None,
    hidden_message: str | None = None,
) -> Mapping[str, Any]:
    """
    Создать счёт на оплату подписки.

    Минимально корректные варианты вызова из бота:

        await create_invoice(message.from_user.id)          # 1 месяц (5 USDT)
        await create_invoice(message.from_user.id, "3m")    # 3 месяца (12 USDT)
        await create_invoice(message.from_user.id, "12m")   # 12 месяцев (60 USDT)
    """
    if plan_code not in PLAN_PRICES:
        raise CryptoPayError(f"Неизвестный план подписки: {plan_code!r}")

    months = PLAN_MONTHS[plan_code]

    # Если явно не передали сумму — берем из тарифов
    if amount is None:
        amount = PLAN_PRICES[plan_code]

    # Текст в CryptoBot
    if description is None:
        description = f"Подписка AI Medicine Premium — {months} мес."

    if hidden_message is None:
        hidden_message = (
            "После оплаты вернись в бота AI Medicine и нажми «Проверить подписку»."
        )

    # payload, которое мы потом разберём при обработке успешной оплаты
    if invoice_payload is None:
        # пример: user:8566723289:plan:3m
        invoice_payload = f"user:{telegram_id}:plan:{plan_code}"

    payload: Dict[str, Any] = {
        "asset": CRYPTO_PAY_ASSET,
        # Crypto Bot ожидает строку в поле amount
        "amount": str(amount),
        "description": description,
        "hidden_message": hidden_message,
        "payload": invoice_payload,
    }

    result = await _crypto_post("createInvoice", payload)
    # result: {"invoice_id":..., "status":..., "pay_url":..., ...}
    return result


async def fetch_invoices_statuses(
    invoice_ids: Iterable[int | str],
) -> Dict[str, Mapping[str, Any]]:
    """
    Получить статусы списка инвойсов.

    :return: dict(invoice_id -> объект инвойса)
    """
    ids = [str(i) for i in invoice_ids]
    if not ids:
        return {}

    payload = {"invoice_ids": ",".join(ids)}
    items = await _crypto_post("getInvoices", payload)

    by_id: Dict[str, Mapping[str, Any]] = {}
    for inv in items:
        by_id[str(inv["invoice_id"])] = inv
    return by_id
