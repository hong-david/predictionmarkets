"""Skip redundant `market_snapshots` rows when the quote is unchanged.

Ticker/poll bursts can repeat identical L1 fields; storing every message
bloats Postgres. We only skip when **all** tracked fields match the latest
row *and* a **heartbeat** window has not elapsed (then we still write so
downstream has a time anchor).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import MarketSnapshot, MarketMetric

_EQ_D = Decimal("0.0001")
_EQ_Q = Decimal("0.01")


def _eq_d(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= _EQ_D


def _eq_q(a: Decimal | None, b: Decimal | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= _EQ_Q


def should_skip_duplicate_snapshot(
    db: Session,
    market_pk: int,
    *,
    last_price_dollars: Decimal | None,
    yes_bid_dollars: Decimal | None,
    yes_ask_dollars: Decimal | None,
    no_bid_dollars: Decimal | None,
    no_ask_dollars: Decimal | None,
    volume_fp: Decimal | None,
    volume_24h_fp: Decimal | None,
    open_interest_fp: Decimal | None,
    liquidity_dollars: Decimal | None,
    now: datetime | None = None,
    heartbeat_seconds: int = 300,
) -> bool:
    """
    Return True to **skip** inserting a new snapshot (duplicate + inside heartbeat).

    Always insert when there is no prior row, when any field changes, or when
    the latest row is older than ``heartbeat_seconds`` (time anchor).
    """
    now = now if now is not None else datetime.now(timezone.utc)
    last = (
        db.query(
            MarketSnapshot.ts,
            MarketSnapshot.last_price_dollars,
            MarketSnapshot.yes_bid_dollars,
            MarketSnapshot.yes_ask_dollars,
            MarketSnapshot.no_bid_dollars,
            MarketSnapshot.no_ask_dollars,
            MarketSnapshot.volume_fp,
            MarketSnapshot.volume_24h_fp,
            MarketSnapshot.open_interest_fp,
            MarketSnapshot.liquidity_dollars,
        )
        .join(MarketMetric, MarketMetric.latest_snapshot_id == MarketSnapshot.id)
        .filter(MarketMetric.market_pk == market_pk)
        .first()
    )
    # Fallback for markets whose metric row has not been initialized yet.
    if last is None:
        last = (
            db.query(
                MarketSnapshot.ts,
                MarketSnapshot.last_price_dollars,
                MarketSnapshot.yes_bid_dollars,
                MarketSnapshot.yes_ask_dollars,
                MarketSnapshot.no_bid_dollars,
                MarketSnapshot.no_ask_dollars,
                MarketSnapshot.volume_fp,
                MarketSnapshot.volume_24h_fp,
                MarketSnapshot.open_interest_fp,
                MarketSnapshot.liquidity_dollars,
            )
            .filter(MarketSnapshot.market_pk == market_pk)
            .order_by(MarketSnapshot.id.desc())
            .first()
        )
    same = (
        _eq_d(last.last_price_dollars, last_price_dollars)
        and _eq_d(last.yes_bid_dollars, yes_bid_dollars)
        and _eq_d(last.yes_ask_dollars, yes_ask_dollars)
        and _eq_d(last.no_bid_dollars, no_bid_dollars)
        and _eq_d(last.no_ask_dollars, no_ask_dollars)
        and _eq_q(last.volume_fp, volume_fp)
        and _eq_q(last.volume_24h_fp, volume_24h_fp)
        and _eq_q(last.open_interest_fp, open_interest_fp)
        and _eq_q(last.liquidity_dollars, liquidity_dollars)
    )
    if not same:
        return False

    last_ts = last.ts
    if last_ts is None:
        return False
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    age = (now - last_ts).total_seconds()
    if age >= heartbeat_seconds:
        return False
    return True
