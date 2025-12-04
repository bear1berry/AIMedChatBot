# bot/payments_crypto.py

import os
import logging
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

CRYPTO_PAY_API_TOKEN = os.getenv("CRYPTO_PAY_API_TOKEN")
CRYPTO_PAY_API_URL = "https://pay.crypt.bot/api/"

class CryptoPayError(Exception):
    """Ошибка работы с Crypto Bot API."""
    pass


# Карта тарифов: код_плана -> цена в USDT
PLAN_PRICES_USDT: dict[str, float] = {
    "month_1": 5.0,    # 1 месяц = 5 USDT
    "month_3": 12.0,   # 3 месяца = 12 USDT
    "month_12": 60.0,  # 12 месяцев = 60 USDT
}


async def _crypto_post(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Вспомогательная функция для запросов к Crypto Bot API.
    """
    if not CRYPTO_PAY_API_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_API_TOKEN не указан в .env")

    url = f"{CRYPTO_PAY_API_URL}{method}"

    headers = {
        "Crypto-Pay-API-Token": CRYPTO_PAY_API_TOKEN,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload, headers=headers)

    try:
        data = resp.json()
    except Exception as e:
        logger.exception("Не удалось распарсить ответ Crypto Bot: %s", resp.text)
        raise CryptoPayError(f"Некорректный ответ от Crypto Bot: {e}") from e

    if not data.get("ok"):
        logger.error("Ошибка Crypto Bot: %s", data)
        raise CryptoPayError(f"Crypto Bot вернул ошибку: {data!r}")

    return data["result"]


async def create_invoice(
    plan_code: str,
    telegram_id: int,
    username: str | None = None,
) -> str:
    """
    Создаёт инвойс в CryptoBot для выбранного плана и возвращает ссылку оплаты.

    :param plan_code: код тарифного плана (должен быть в PLAN_PRICES_USDT)
    :param telegram_id: Telegram ID пользователя
    :param username: username пользователя (опционально, для payload/описания)
    :return: URL для оплаты через CryptoBot
    """
    # Находим цену для тарифа
    price = PLAN_PRICES_USDT.get(plan_code)
    if price is None:
        raise CryptoPayError(f"Неизвестный тарифный план: {plan_code}")

    # Всегда выставляем USDT
    asset = "USDT"

    # Красивое описание для CryptoBot
    if plan_code == "month_1":
        desc = "Премиум-доступ к AI-ассистенту на 1 месяц"
    elif plan_code == "month_3":
        desc = "Премиум-доступ к AI-ассистенту на 3 месяца"
    elif plan_code == "month_12":
        desc = "Премиум-доступ к AI-ассистенту на 12 месяцев"
    else:
        desc = "Премиум-доступ к AI-ассистенту"

    user_tag = f"@{username}" if username else f"id:{telegram_id}"

    # payload — то, что вернётся тебе в вебхуке / при проверке статуса
    payload = f"user={telegram_id};plan={plan_code};user_tag={user_tag}"

    request_payload: Dict[str, Any] = {
        "asset": asset,          # <--- ВАЖНО: USDT
        "amount": float(price),  # <--- 5 / 12 / 60
        "description": desc,
        "payload": payload,
        "allow_anonymous": False,
        "allow_comments": False,
        # "expires_in": 3600,    # можно включить, если нужна ограниченная по времени ссылка
    }

    result = await _crypto_post("createInvoice", request_payload)
    pay_url = result.get("pay_url")

    if not pay_url:
        logger.error("У ответа Crypto Bot нет pay_url: %s", result)
        raise CryptoPayError("Crypto Bot не вернул ссылку на оплату")

    logger.info(
        "Создан инвойс для %s (%s), план=%s, сумма=%s %s, pay_url=%s",
        telegram_id,
        user_tag,
        plan_code,
        price,
        asset,
        pay_url,
    )

    return pay_url
