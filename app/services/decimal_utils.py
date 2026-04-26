"""Small parsing helpers shared by ingestion paths."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def parse_decimal(value: Any) -> Decimal | None:
    """Parse exchange numeric payloads into Decimal, preserving nulls.

    Kalshi payloads are usually strings, but tests and future callers may pass
    ints/floats/Decimals. Converting through ``str`` avoids binary-float
    artifacts leaking into Decimal when a numeric value is supplied.
    """
    if value is None or value == "":
        return None
    return Decimal(str(value))
