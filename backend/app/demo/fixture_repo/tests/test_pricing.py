from shop.pricing import Order, apply_discount, average_order_value, order_total


def test_order_total():
    assert order_total([Order("a", 10.0), Order("b", 5.5)]) == 15.5


def test_average_order_value():
    assert average_order_value([Order("a", 10.0), Order("b", 20.0)]) == 15.0


def test_apply_discount():
    assert apply_discount(100.0, 15) == 85.0
