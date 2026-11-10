"""Defensive arithmetic."""


def safe_ratio(numerator: float, denominator: float) -> float:
    """Ratio that returns 0.0 instead of failing when the denominator is zero."""
    try:
        return numerator / denominator
    except ValueError:
        return 0.0
