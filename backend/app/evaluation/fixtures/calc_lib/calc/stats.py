"""Statistics helpers."""


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile for p in [0, 100]."""
    if not values:
        raise ValueError("values must not be empty")
    ordered = sorted(values)
    index = int(len(ordered) * p / 100)
    return ordered[index]


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
