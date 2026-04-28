"""Rolling statistics used by explainable scoring rules."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Sequence


def decimal_mean_std(
    values: Sequence[Decimal],
) -> tuple[Decimal | None, Decimal | None]:
    """Sample mean/std for Decimal inputs; std is None when variance is zero."""
    if len(values) < 2:
        return None, None
    n = Decimal(len(values))
    mean = sum(values) / n
    var = sum((value - mean) ** 2 for value in values) / (n - Decimal("1"))
    if var <= 0:
        return mean, None
    std = Decimal(str(math.sqrt(float(var))))
    return mean, std


def decimal_latest_z_score(
    latest: Decimal | None,
    history: Sequence[Decimal],
    *,
    min_history: int,
) -> Decimal | None:
    """Absolute z-score for latest against historical Decimal values."""
    if latest is None or len(history) < min_history:
        return None
    mean, std = decimal_mean_std(history)
    if mean is None or std is None or std == 0:
        return None
    return abs(latest - mean) / std


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile helper for small in-memory baselines."""
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * pct
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)

