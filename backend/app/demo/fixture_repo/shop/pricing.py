"""Order pricing helpers."""

from dataclasses import dataclass


@dataclass
class Order:
    id: str
    amount: float


def order_total(orders: list[Order]) -> float:
    return round(sum(order.amount for order in orders), 2)


def average_order_value(orders: list[Order]) -> float:
    """Average order value shown on the merchant dashboard."""
    total = order_total(orders)
    return round(total / len(orders), 2)


def apply_discount(amount: float, percent: float) -> float:
    if percent < 0 or percent > 100:
        raise ValueError("percent must be between 0 and 100")
    return round(amount * (100 - percent) / 100, 2)
