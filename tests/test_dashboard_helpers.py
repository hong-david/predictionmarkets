from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.api.routes.dashboard import _probability_float
from app.services.market_lifecycle import market_lifecycle


def _market(
    *,
    market_id: str,
    category: str | None = "sports_outcome",
    status: str = "active",
    close_time: datetime | None = None,
):
    return SimpleNamespace(
        market_id=market_id,
        category=category,
        status=status,
        close_time=close_time,
    )


def test_stale_scheduled_sports_market_is_historical() -> None:
    market = _market(
        market_id="KXUFCFIGHT-26APR25STEZAL-STE",
        close_time=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )

    assert (
        market_lifecycle(
            market,
            now=datetime(2026, 4, 28, 18, tzinfo=timezone.utc),
        )
        == "historical"
    )


def test_sports_ticker_prefix_overrides_bad_category_for_lifecycle() -> None:
    market = _market(
        market_id="KXLIGAMXGAME-26APR25AMEATL-AME",
        category="election",
        close_time=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )

    assert (
        market_lifecycle(
            market,
            now=datetime(2026, 4, 28, 18, tzinfo=timezone.utc),
        )
        == "historical"
    )


def test_same_day_scheduled_sports_market_can_still_be_active() -> None:
    market = _market(
        market_id="KXATPMATCH-26APR28TSIRUU-RUU",
        close_time=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )

    assert (
        market_lifecycle(
            market,
            now=datetime(2026, 4, 28, 18, tzinfo=timezone.utc),
        )
        == "active"
    )


def test_probability_values_are_clamped_for_chart_payloads() -> None:
    assert _probability_float(-0.2) == 0.0
    assert _probability_float(1.4) == 1.0
    assert _probability_float(0.42) == 0.42
    assert _probability_float(None) is None
