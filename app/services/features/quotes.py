"""Quote-derived feature helpers shared by alerting and trade scoring."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence


def finite_float(value: Any) -> float | None:
    """Coerce a JSON/DB scalar to a finite float, returning None on bad input."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def parse_iso_datetime(raw: Any) -> datetime | None:
    """Parse an ISO timestamp and normalize naive values to UTC."""
    if not raw:
        return None
    value = str(raw).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(value)
    except ValueError:
        return None
    if out.tzinfo is None:
        return out.replace(tzinfo=timezone.utc)
    return out


def decimal_spread(
    bid: Decimal | None,
    ask: Decimal | None,
) -> Decimal | None:
    if bid is None or ask is None:
        return None
    return ask - bid


def decimal_mid_price(
    bid: Decimal | None,
    ask: Decimal | None,
) -> Decimal | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / Decimal("2")


def decimal_reference_price(
    *,
    last: Decimal | None,
    bid: Decimal | None,
    ask: Decimal | None,
) -> Decimal | None:
    if last is not None and last > 0:
        return last
    return decimal_mid_price(bid, ask)


def row_reference_price(row: Mapping[str, Any]) -> float | None:
    """Reference yes price for dict-shaped snapshot rows used by trade context."""
    last = finite_float(row.get("last_price"))
    if last is not None and last > 0:
        return last
    bid = finite_float(row.get("yes_bid"))
    ask = finite_float(row.get("yes_ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return None


def row_quote_spread(row: Mapping[str, Any]) -> float | None:
    """Non-negative yes bid/ask spread for dict-shaped snapshot rows."""
    bid = finite_float(row.get("yes_bid"))
    ask = finite_float(row.get("yes_ask"))
    if bid is None or ask is None:
        return None
    return max(0.0, ask - bid)


def quote_at_or_before(
    snapshots: Sequence[Mapping[str, Any]],
    ts: datetime,
) -> Mapping[str, Any] | None:
    """Return the latest snapshot row at or before ``ts``.

    Input rows are expected in ascending timestamp order, which matches the
    market-series payload built for contextual trade scoring.
    """
    best: Mapping[str, Any] | None = None
    for row in snapshots:
        row_ts = parse_iso_datetime(row.get("ts"))
        if row_ts is None:
            continue
        if row_ts <= ts:
            best = row
        else:
            break
    return best

