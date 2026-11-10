from calc.safe import safe_ratio


def test_safe_ratio():
    assert safe_ratio(6, 3) == 2.0
