from __future__ import annotations

import os
import logging
from typing import Any, Mapping, Dict

import httpx

logger = logging.getLogger(__name__)

# Токен и URL Crypto Pay API
CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api").rstrip("/")

# Актив, в котором выставляется счёт: TON / USDT / и т.д.
# По умолчанию TON, чтобы точно работало с твоим мерчантом
CRYPTO_PAY_ASSET = (os.getenv("CRYPTO_PAY_ASSET") or "TON").upper().strip() or "TON"

# Количество месяцев по кодам тарифов
PLAN_MONTHS: Dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "12m": 12,
}

# Цены по тарифам (в единицах CRYPTO_PAY_ASSET)
# 5 / 12 / 60 — как ты просил
PLAN_PRICES: Dict[str, float] = {
    "1m": 5.0,
    "3m": 12.0,
    "12m": 60.0,
}


class CryptoPayError(RuntimeError):
    """Любая ошибка при работе с Crypto Pay API."""


async def _crypto_post(method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """
    Низкоуровневый вызов Crypto Pay API.

    :param method: имя метода, например 'createInvoice'
    :param payload: JSON-параметры запроса
    :return: поле 'result' из ответа CryptoBot
    """
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("Environment variable CRYPTO_PAY_API_TOKEN is not set")

    url = f"{CRYPTO_PAY_API_URL}/{method}"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={"Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN},
        )

    try:
        data = resp.json()
    except Exception as exc:
        logger.exception("CRYPTOBOT: invalid JSON response: %s", resp.text)
        raise CryptoPayError("Invalid response from CryptoBot") from exc

    if not data.get("ok"):
        # Здесь будет видно, что именно вернул CryptoBot
        logger.error("CRYPTOBOT ERROR %s: %s", method, data)
        raise CryptoPayError(str(data.get("error") or data))

    result = data.get("result")
    if result is None:
        raise CryptoPayError("CryptoBot response has no 'result' field")

    return result


async def create_invoice(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
    """
    Гибкая обёртка над createInvoice.

    Поддерживает разные варианты вызова (чтобы не ломать твой текущий код):

        await create_invoice("1m")
        await create_invoice(user_id, "1m")
        await create_invoice(user_id=user_id, plan_code="1m", description="...")
        await create_invoice(plan="1m", amount=5)

    Возвращает словарь result из CryptoBot, в котором есть:
      - invoice_id
      - pay_url
      - bot_invoice_url
      и т.п.
    """
    # ---------- определяем plan_code ----------
    plan_code = kwargs.get("plan_code") or kwargs.get("plan")
    if plan_code is None:
        # Если план передали позиционно: (user_id, "1m") или просто ("1m",)
        if len(args) >= 2 and isinstance(args[1], str):
            plan_code = args[1]
        elif len(args) >= 1 and isinstance(args[0], str):
            plan_code = args[0]

    if not isinstance(plan_code, str):
        raise CryptoPayError(
            f"Unsupported call to create_invoice, can't detect plan_code; "
            f"args={args}, kwargs={kwargs}"
        )

    plan_code = plan_code.strip()
    if plan_code not in PLAN_PRICES:
        raise CryptoPayError(f"Unknown subscription plan: {plan_code}")

    # ---------- определяем сумму ----------
    amount = kwargs.get("amount") or kwargs.get("price") or PLAN_PRICES[plan_code]
    try:
        amount_value = float(amount)
    except (TypeError, ValueError):
        raise CryptoPayError(f"Invalid amount: {amount!r}")

    if amount_value <= 0:
        raise CryptoPayError("Amount must be positive")

    # ---------- описание ----------
    description = kwargs.get("description") or f"Подписка AI Medicine Premium — план {plan_code}"
    hidden_message = kwargs.get("hidden_message") or (
        "После оплаты вернись в бот — подписка активируется автоматически."
    )

    payload: dict[str, Any] = {
        "asset": CRYPTO_PAY_ASSET,
        "amount": str(amount_value),
        "description": description[:1024],
        "hidden_message": hidden_message[:4096],
        "expires_in": 3600,  # 1 час на оплату
    }

    # Опциональное поле payload — удобно привязать invoice к пользователю
    invoice_payload = kwargs.get("invoice_payload")
    if invoice_payload is None and args:
        # Если первым аргументом передали user_id (int) — используем его
        if isinstance(args[0], int):
            invoice_payload = f"user:{args[0]}:{plan_code}"

    if invoice_payload is not None:
        payload["payload"] = str(invoice_payload)[:128]

    logger.info(
        "CRYPTOBOT: creating invoice method=createInvoice asset=%s amount=%s plan=%s payload=%r",
        CRYPTO_PAY_ASSET,
        amount_value,
        plan_code,
        payload.get("payload"),
    )

    result = await _crypto_post("createInvoice", payload)
    return result


async def fetch_invoices_statuses(*args: Any, **kwargs: Any) -> dict[str, Mapping[str, Any]]:
    """
    Получить статусы инвойсов из CryptoBot.

    Допустимые варианты вызова:

        await fetch_invoices_statuses(["123", "456"])
        await fetch_invoices_statuses(invoice_ids=["123", "456"])

    Возвращает словарь:
        { "123": invoice_dict, "456": invoice_dict, ... }
    """
    invoice_ids = kwargs.get("invoice_ids")
    if invoice_ids is None and args:
        invoice_ids = args[0]

    if not invoice_ids:
        return {}

    ids_list = [str(i) for i in invoice_ids]

    payload = {"invoice_ids": ids_list}

    logger.info("CRYPTOBOT: fetching invoices statuses for %s", ids_list)

    result = await _crypto_post("getInvoices", payload)

    # CryptoBot возвращает список инвойсов
    by_id: dict[str, Mapping[str, Any]] = {}
    if isinstance(result, list):
        for inv in result:
            inv_id = str(inv.get("invoice_id") or inv.get("id"))
            if inv_id:
                by_id[inv_id] = inv

    return by_id


__all__ = [
    "CryptoPayError",
    "PLAN_MONTHS",
    "PLAN_PRICES",
    "create_invoice",
    "fetch_invoices_statuses",
]
