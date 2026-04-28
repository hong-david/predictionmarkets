"""Liquidity and size feature helpers."""

from __future__ import annotations

from decimal import Decimal


def decimal_volume_delta(
    latest_volume: Decimal | None,
    previous_volume: Decimal | None,
) -> Decimal | None:
    if latest_volume is None or previous_volume is None:
        return None
    return latest_volume - previous_volume


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator

