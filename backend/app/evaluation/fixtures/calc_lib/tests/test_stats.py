from calc.stats import mean, percentile


def test_percentile_median():
    assert percentile([5, 1, 3], 50) == 3


def test_mean():
    assert mean([1, 2, 3]) == 2.0
