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
    "month": SubscriptionPlan(
        code="month",
        title="Подписка на 30 дней",
        description=(
            "Полный доступ ко всем возможностям бота: длинные запросы, "
            "глубокие разборы и приоритетная обработка."
        ),
        days=30,
        price_ton=5.0,   # эквивалент 5$
        price_usdt=5.0,  # эквивалент 5$
    ),
}


def get_plan(code: str) -> SubscriptionPlan | None:
    return PLANS.get(code)
