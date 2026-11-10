"""Merchant dashboard view models."""

from shop.pricing import Order, average_order_value


def merchant_summary(orders: list[Order]) -> dict[str, float]:
    return {
        "orders": len(orders),
        "average_order_value": average_order_value(orders),
    }
