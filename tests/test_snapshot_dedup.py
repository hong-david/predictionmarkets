from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.services.snapshot_dedup import should_skip_duplicate_snapshot


def _chain_query(db: MagicMock, first_return) -> None:
    m = MagicMock()
    db.query.return_value = m
    m.filter.return_value = m
    m.order_by.return_value = m
    m.first.return_value = first_return


def test_inserts_first_row_no_prior() -> None:
    db = MagicMock()
    _chain_query(db, None)
    assert (
        should_skip_duplicate_snapshot(
            db,
            1,
            last_price_dollars=Decimal("0.5"),
            yes_bid_dollars=Decimal("0.4"),
            yes_ask_dollars=Decimal("0.6"),
            no_bid_dollars=None,
            no_ask_dollars=None,
            volume_fp=Decimal("100"),
            volume_24h_fp=Decimal("10"),
            open_interest_fp=None,
            liquidity_dollars=None,
        )
        is False
    )


def test_skips_identical_when_recent() -> None:
    last = MagicMock()
    last.last_price_dollars = Decimal("0.83")
    last.yes_bid_dollars = Decimal("0.80")
    last.yes_ask_dollars = Decimal("0.85")
    last.no_bid_dollars = None
    last.no_ask_dollars = None
    last.volume_fp = Decimal("1000")
    last.volume_24h_fp = Decimal("100")
    last.open_interest_fp = None
    last.liquidity_dollars = None
    last.ts = datetime.now(timezone.utc) - timedelta(seconds=30)

    db = MagicMock()
    _chain_query(db, last)
    now = last.ts + timedelta(seconds=30)
    assert (
        should_skip_duplicate_snapshot(
            db,
            1,
            last_price_dollars=Decimal("0.83"),
            yes_bid_dollars=Decimal("0.80"),
            yes_ask_dollars=Decimal("0.85"),
            no_bid_dollars=None,
            no_ask_dollars=None,
            volume_fp=Decimal("1000"),
            volume_24h_fp=Decimal("100"),
            open_interest_fp=None,
            liquidity_dollars=None,
            now=now,
        )
        is True
    )


def test_heartbeat_inserts_after_window() -> None:
    last = MagicMock()
    last.last_price_dollars = Decimal("0.5")
    last.yes_bid_dollars = None
    last.yes_ask_dollars = None
    last.no_bid_dollars = None
    last.no_ask_dollars = None
    last.volume_fp = None
    last.volume_24h_fp = None
    last.open_interest_fp = None
    last.liquidity_dollars = None
    last.ts = datetime.now(timezone.utc) - timedelta(seconds=400)

    db = MagicMock()
    _chain_query(db, last)
    assert (
        should_skip_duplicate_snapshot(
            db,
            1,
            last_price_dollars=Decimal("0.5"),
            yes_bid_dollars=None,
            yes_ask_dollars=None,
            no_bid_dollars=None,
            no_ask_dollars=None,
            volume_fp=None,
            volume_24h_fp=None,
            open_interest_fp=None,
            liquidity_dollars=None,
            now=datetime.now(timezone.utc),
            heartbeat_seconds=300,
        )
        is False
    )


def test_field_change_forces_row() -> None:
    last = MagicMock()
    last.last_price_dollars = Decimal("0.5")
    last.yes_bid_dollars = None
    last.yes_ask_dollars = None
    last.no_bid_dollars = None
    last.no_ask_dollars = None
    last.volume_fp = None
    last.volume_24h_fp = None
    last.open_interest_fp = None
    last.liquidity_dollars = None
    last.ts = datetime.now(timezone.utc) - timedelta(seconds=30)

    db = MagicMock()
    _chain_query(db, last)
    assert (
        should_skip_duplicate_snapshot(
            db,
            1,
            last_price_dollars=Decimal("0.94"),
            yes_bid_dollars=None,
            yes_ask_dollars=None,
            no_bid_dollars=None,
            no_ask_dollars=None,
            volume_fp=None,
            volume_24h_fp=None,
            open_interest_fp=None,
            liquidity_dollars=None,
        )
        is False
    )
