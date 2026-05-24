from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.services import market_price_history as history
from app.services import quote_series


def test_quote_history_gate_throttles_within_same_bucket() -> None:
    history._last_quote_write_by_market_bucket.clear()
    t0 = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    assert history.should_write_quote_history(1, t0, now_m=0.0) is True
    assert (
        history.should_write_quote_history(
            1, t0 + timedelta(seconds=30), now_m=30.0
        )
        is False
    )
    assert (
        history.should_write_quote_history(
            1, t0 + timedelta(seconds=90), now_m=90.0
        )
        is True
    )


def test_quote_history_gate_prunes_old_bucket_entries(monkeypatch) -> None:
    history._last_quote_write_by_market_bucket.clear()
    monkeypatch.setattr(history, "_QUOTE_CACHE_PRUNE_INTERVAL_SEC", 1.0)
    monkeypatch.setattr(history, "_QUOTE_CACHE_MAX_AGE_BUCKETS", 1)

    t0 = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=10)

    assert history.should_write_quote_history(1, t0, now_m=0.0) is True
    old_key = (1, history.DEFAULT_CHART_HISTORY_INTERVAL_SEC, history.bucket_start(t0))
    assert old_key in history._last_quote_write_by_market_bucket

    assert history.should_write_quote_history(1, t1, now_m=2.0) is True
    assert old_key not in history._last_quote_write_by_market_bucket


def test_history_point_adapts_chart_history_for_snapshot_free_consumers() -> None:
    bucket = datetime(2026, 5, 18, 4, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(
        market_pk=7,
        interval_sec=300,
        bucket_start=bucket,
        close_price_dollars=Decimal("0.39"),
        close_price_source="trade",
        close_yes_bid_dollars=Decimal("0.38"),
        close_yes_ask_dollars=Decimal("0.40"),
        close_volume_24h_fp=Decimal("1200"),
        close_open_interest_fp=Decimal("800"),
        last_price_ts=bucket + timedelta(minutes=3),
        last_quote_ts=bucket + timedelta(minutes=4),
        trade_volume_contracts=Decimal("25"),
        trade_count=3,
        quote_count=2,
    )

    point = quote_series.history_point(row)
    payload = quote_series.quote_payload(point)

    assert point.id is None
    assert point.source == "chart_history"
    assert point.source_key == "7:300:2026-05-18T04:00:00+00:00"
    assert point.ts == bucket + timedelta(minutes=4)
    assert point.last_price_dollars == Decimal("0.39")
    assert point.volume_fp == Decimal("25")
    assert quote_series.display_price(point) == 0.39
    assert payload["source"] == "chart_history"
    assert payload["last_price"] == 0.39


def test_history_point_uses_midpoint_when_close_price_is_quote_derived() -> None:
    bucket = datetime(2026, 5, 18, 4, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(
        market_pk=7,
        interval_sec=300,
        bucket_start=bucket,
        close_price_dollars=Decimal("0.50"),
        close_price_source="midpoint",
        close_yes_bid_dollars=Decimal("0.38"),
        close_yes_ask_dollars=Decimal("0.40"),
        close_volume_24h_fp=None,
        close_open_interest_fp=None,
        last_price_ts=None,
        last_quote_ts=None,
        trade_volume_contracts=None,
        trade_count=0,
        quote_count=1,
    )

    point = quote_series.history_point(row)

    assert point.last_price_dollars is None
    assert quote_series.display_price(point) == 0.39
