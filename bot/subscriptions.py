# bot/subscriptions.py

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class SubscriptionPlan:
    code: str
    title: str
    description: str
    days: int
    price_ton: float
    price_usdt: float


PLANS: Dict[str, SubscriptionPlan] = {
    "week": SubscriptionPlan(
        code="week",
        title="Подписка на 7 дней",
        description="Полный доступ ко всем функциям бота в течение 7 дней.",
        days=7,
        price_ton=3.0,
        price_usdt=3.0,
    ),
    "month": SubscriptionPlan(
        code="month",
        title="Подписка на 30 дней",
        description="Максимальный доступ ко всем запросам бота на 30 дней.",
        days=30,
        price_ton=9.0,
        price_usdt=9.0,
    ),
    "quarter": SubscriptionPlan(
        code="quarter",
        title="Подписка на 90 дней",
        description="Выгодный тариф: 90 дней безлимитных запросов.",
        days=90,
        price_ton=24.0,
        price_usdt=24.0,
    ),
}


def get_plan(code: str) -> SubscriptionPlan | None:
    return PLANS.get(code)
