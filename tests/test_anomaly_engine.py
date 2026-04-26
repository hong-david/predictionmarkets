"""Unit tests for rolling-baseline logic in `anomaly_engine` (no DB)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.services.anomaly_engine import analyze_market


def _snap(
    *,
    bid: str,
    ask: str,
    last: str | None,
    vol: str = "100",
    liq: str = "50",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        ts=None,
        yes_bid_dollars=Decimal(bid),
        yes_ask_dollars=Decimal(ask),
        last_price_dollars=Decimal(last) if last else None,
        volume_fp=Decimal(vol),
        liquidity_dollars=Decimal(liq),
    )


def test_rolling_wide_spread_on_latest() -> None:
    """Many similar tight spreads, then one very wide spread on the latest row."""
    stable = [_snap(bid="0.45", ask="0.55", last="0.50") for _ in range(10)]
    latest = _snap(bid="0.10", ask="0.90", last="0.50", liq="50")
    snaps = [latest] + stable[1:]  # newest first: [0] = outlier
    market = SimpleNamespace(market_id="KXTEST", title="Test")
    out = analyze_market(market, snaps, book_activity=None)
    assert out["score"] > 0
    assert any("wide spread" in r for r in out["reasons"])


def test_book_activity_bumps_score() -> None:
    snaps = [_snap(bid="0.48", ask="0.52", last="0.50") for _ in range(6)]
    market = SimpleNamespace(market_id="KXTEST2", title="Test2")
    book = {
        "window_minutes": 3,
        "book_events_3m": 500,
        "book_deltas_3m": 100,
        "pull_volume_3m": 10.0,
        "high_churn": True,
        "heavy_cancel_flex": False,
    }
    out = analyze_market(market, snaps, book_activity=book)
    assert out["score"] > 0
    assert "book_activity" in out["signals"]
