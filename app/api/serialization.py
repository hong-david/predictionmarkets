"""Shared JSON serialization helpers for legacy API routes."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal


def isoformat_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def decimal_to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None
