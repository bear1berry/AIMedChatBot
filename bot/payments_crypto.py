import os
import json
import time
import logging
from typing import Any, Dict, List, Tuple

import httpx

logger = logging.getLogger(__name__)

# === ENV ===

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN", "").strip()
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api").rstrip("/")

# Актив, в котором выставляем счёт.
# По умолчанию TON, потому что мы уже знаем, что он у тебя работает.
# Если позже включишь USDT в Crypto Bot – можешь добавить в .env:
#   CRYPTO_PAY_ASSET=USDT
CRYPTO_PAY_ASSET = os.getenv("CRYPTO_PAY_ASSET", "TON").strip() or "TON"


class CryptoPayError(Exception):
    """Ошибка работы с Crypto Bot API."""


# === Настройки тарифов ===
#
# Один и тот же тариф может иметь несколько кодов, чтобы не ломать
# существующую логику в других файлах (на всякий случай).

PLAN_AMOUNTS: Dict[str, str] = {
    # 1 месяц
    "month": "5",
    "m1": "5",
    "1m": "5",
    "premium_month": "5",

    # 3 месяца
    "3months": "12",
    "m3": "12",
    "3m": "12",
    "premium_3m": "12",

    # 12 месяцев
    "year": "60",
    "12m": "60",
    "m12": "60",
    "premium_12m": "60",
}

PLAN_TITLES: Dict[str, str] = {
    "month": "1 месяц",
    "m1": "1 месяц",
    "1m": "1 месяц",
    "premium_month": "1 месяц",
    "3months": "3 месяца",
    "m3": "3 месяца",
    "3m": "3 месяца",
    "premium_3m": "3 месяца",
    "year": "12 месяцев",
    "12m": "12 месяцев",
    "m12": "12 месяцев",
    "premium_12m": "12 месяцев",
}

DEFAULT_AMOUNT = "5"
DEFAULT_TITLE = "1 месяц"


# === Вспомогательный запрос к Crypto Bot ===

async def _crypto_post(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Вызов метода Crypto Bot API.

    :param method: имя метода, например 'createInvoice' или 'getInvoices'
    :param params: словарь параметров
    :return: словарь result из ответа API
    :raises CryptoPayError: при сетевой ошибке или ошибке API
    """
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN не задан в .env")

    url = f"{CRYPTO_PAY_API_URL}/{method}"

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
        "Content-Type": "application/json",
    }

    logger.info("CryptoBot request %s params=%s", method, params)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, headers=headers, json=params)
    except Exception as exc:
        logger.exception("Network error while calling Crypto Bot API")
        raise CryptoPayError(f"Сетевая ошибка при обращении к Crypto Bot: {exc}") from exc

    if response.status_code != 200:
        logger.error("Crypto Bot HTTP %s: %s", response.status_code, response.text)
        raise CryptoPayError(
            f"Crypto Bot вернул HTTP {response.status_code}: {response.text}"
        )

    try:
        data = response.json()
    except Exception as exc:
        logger.error("Cannot parse Crypto Bot response as JSON: %s", response.text)
        raise CryptoPayError(f"Некорректный ответ от Crypto Bot: {exc}") from exc

    if not data.get("ok"):
        # Логируем подробности, чтобы их можно было посмотреть через journalctl
        logger.error("Crypto Bot API error: %r", data)
        raise CryptoPayError(f"Crypto Bot вернул ошибку: {data!r}")

    result = data.get("result")
    if not isinstance(result, dict):
        logger.error("Unexpected Crypto Bot 'result': %r", result)
        raise CryptoPayError("Некорректный формат ответа Crypto Bot (result)")

    return result


# === Публичные функции, которые используются в других модулях ===

async def create_invoice(
    plan_code: str,
    telegram_id: int,
    username: str | None = None,
) -> Tuple[str, str]:
    """
    Создать счёт в Crypto Bot.

    :param plan_code: код плана (month / 3months / year, либо твой старый код)
    :param telegram_id: id пользователя в Telegram
    :param username: username пользователя (для описания)
    :return: (bot_invoice_url, invoice_id) — ссылка на счёт и его id
    :raises CryptoPayError: если что-то пошло не так
    """

    # Цена и подпись тарифа по коду. Если код неизвестен — считаем, что это 1 месяц.
    amount = PLAN_AMOUNTS.get(plan_code, DEFAULT_AMOUNT)
    title_suffix = PLAN_TITLES.get(plan_code, DEFAULT_TITLE)

    description = f"Подписка AI Medicine Premium — {title_suffix}"

    # payload, чтобы потом можно было сопоставить платёж с пользователем/тарифом
    payload = json.dumps(
        {
            "u": telegram_id,
            "p": plan_code,
            "ts": int(time.time()),
        },
        ensure_ascii=False,
    )

    params = {
        "asset": CRYPTO_PAY_ASSET,      # по умолчанию TON
        "amount": amount,               # строкой, как рекомендует большинство API
        "description": description,
        "payload": payload,
        "allow_comments": False,
        "allow_anonymous": False,
        # счёт живёт 1 час; можно изменить при желании
        "expires_in": 3600,
    }

    result = await _crypto_post("createInvoice", params)

    invoice_id = str(result.get("invoice_id"))
    bot_invoice_url = result.get("bot_invoice_url") or result.get("pay_url")

    if not invoice_id or not bot_invoice_url:
        logger.error("Unexpected createInvoice result: %r", result)
        raise CryptoPayError("Crypto Bot вернул некорректные данные при создании счёта")

    logger.info(
        "Created invoice id=%s plan=%s amount=%s asset=%s for user=%s",
        invoice_id,
        plan_code,
        amount,
        CRYPTO_PAY_ASSET,
        telegram_id,
    )

    # ВАЖНО: возвращаем tuple (url, id), как ожидает существующий код.
    return bot_invoice_url, invoice_id


async def fetch_invoices_statuses(invoice_ids: List[str]) -> Dict[str, str]:
    """
    Получить статусы нескольких счетов.

    :param invoice_ids: список id счетов Crypto Bot (строки)
    :return: словарь {invoice_id: status}
    :raises CryptoPayError: при ошибке запроса
    """
    clean_ids = [str(i).strip() for i in invoice_ids if str(i).strip()]
    if not clean_ids:
        return {}

    params = {"invoice_ids": ",".join(clean_ids)}

    result = await _crypto_post("getInvoices", params)

    # В ответе от Crypto Bot result — это словарь с ключом "items"
    items = result.get("items")
    if not isinstance(items, list):
        logger.error("Unexpected getInvoices result: %r", result)
        raise CryptoPayError("Crypto Bot вернул некорректные данные при запросе статусов")

    statuses: Dict[str, str] = {}
    for item in items:
        try:
            inv_id = str(item["invoice_id"])
            status = str(item["status"])
        except Exception:
            logger.warning("Bad invoice item in response: %r", item)
            continue

        statuses[inv_id] = status

    logger.info("Fetched invoice statuses: %s", statuses)
    return statuses
