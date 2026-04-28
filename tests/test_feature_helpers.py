from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.services.features.liquidity import decimal_volume_delta, safe_ratio
from app.services.features.quotes import (
    decimal_mid_price,
    decimal_reference_price,
    decimal_spread,
    quote_at_or_before,
    row_quote_spread,
    row_reference_price,
)
from app.services.features.rolling import decimal_latest_z_score, percentile


def test_decimal_quote_helpers() -> None:
    assert decimal_spread(Decimal("0.40"), Decimal("0.55")) == Decimal("0.15")
    assert decimal_mid_price(Decimal("0.40"), Decimal("0.60")) == Decimal("0.50")
    assert (
        decimal_reference_price(
            last=Decimal("0.42"),
            bid=Decimal("0.30"),
            ask=Decimal("0.70"),
        )
        == Decimal("0.42")
    )
    assert (
        decimal_reference_price(
            last=Decimal("0"),
            bid=Decimal("0.30"),
            ask=Decimal("0.70"),
        )
        == Decimal("0.50")
    )


def test_row_quote_helpers_and_quote_lookup() -> None:
    rows = [
        {
            "ts": "2026-01-01T10:00:00+00:00",
            "yes_bid": 0.40,
            "yes_ask": 0.50,
            "last_price": 0.0,
        },
        {
            "ts": "2026-01-01T10:05:00+00:00",
            "yes_bid": 0.45,
            "yes_ask": 0.55,
            "last_price": 0.52,
        },
    ]

    assert row_reference_price(rows[0]) == 0.45
    assert row_reference_price(rows[1]) == 0.52
    assert round(row_quote_spread(rows[1]) or 0.0, 4) == 0.10
    assert (
        quote_at_or_before(
            rows,
            datetime(2026, 1, 1, 10, 2, tzinfo=timezone.utc),
        )
        == rows[0]
    )


def test_rolling_and_liquidity_helpers() -> None:
    z = decimal_latest_z_score(
        Decimal("10"),
        [Decimal("1"), Decimal("1"), Decimal("2"), Decimal("2"), Decimal("3")],
        min_history=5,
    )
    assert z is not None and z > 5
    assert percentile([10.0, 20.0, 30.0], 0.5) == 20.0
    assert decimal_volume_delta(Decimal("150"), Decimal("100")) == Decimal("50")
    assert safe_ratio(25.0, 100.0) == 0.25
    assert safe_ratio(25.0, 0.0) is None
