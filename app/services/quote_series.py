"""Snapshot-free quote/history adapters.

The runtime pipeline no longer needs ``market_snapshots`` as the serving shape.
These helpers expose compact ``market_metrics`` and ``market_price_history`` rows
with the same quote fields expected by the scoring and dashboard code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.db.models import Market, MarketMetric, MarketPriceHistory


@dataclass(frozen=True)
class QuotePoint:
    market_pk: int
    ts: datetime | None
    last_price_dollars: Decimal | None = None
    yes_bid_dollars: Decimal | None = None
    yes_ask_dollars: Decimal | None = None
    no_bid_dollars: Decimal | None = None
    no_ask_dollars: Decimal | None = None
    volume_fp: Decimal | None = None
    volume_24h_fp: Decimal | None = None
    open_interest_fp: Decimal | None = None
    liquidity_dollars: Decimal | None = None
    source: str = "unknown"
    source_key: str | None = None
    interval_sec: int | None = None
    trade_count: int = 0
    quote_count: int = 0

    @property
    def id(self) -> int | None:
        # Compatibility for legacy code paths that still inspect ``.id``.
        return None


def cents_to_dollars(value: int | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value) / Decimal("100")


def contracts_to_decimal(value: int | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value)


def history_point(row: MarketPriceHistory) -> QuotePoint:
    ts = row.last_quote_ts or row.last_price_ts or row.bucket_start
    last_price = (
        row.close_price_dollars
        if row.close_price_source in {"trade", "last_price"}
        else None
    )
    bucket_volume = (
        row.trade_volume_contracts
        if row.trade_volume_contracts is not None and int(row.trade_count or 0) > 0
        else None
    )
    return QuotePoint(
        market_pk=int(row.market_pk),
        ts=ts,
        last_price_dollars=last_price,
        yes_bid_dollars=row.close_yes_bid_dollars,
        yes_ask_dollars=row.close_yes_ask_dollars,
        volume_fp=bucket_volume,
        volume_24h_fp=row.close_volume_24h_fp,
        open_interest_fp=row.close_open_interest_fp,
        source="chart_history",
        source_key=(
            f"{int(row.market_pk)}:{int(row.interval_sec)}:"
            f"{row.bucket_start.isoformat() if row.bucket_start else ''}"
        ),
        interval_sec=int(row.interval_sec),
        trade_count=int(row.trade_count or 0),
        quote_count=int(row.quote_count or 0),
    )


def metric_point(metric: MarketMetric) -> QuotePoint:
    yes_bid = cents_to_dollars(metric.yes_bid_cents)
    yes_ask = cents_to_dollars(metric.yes_ask_cents)
    return QuotePoint(
        market_pk=int(metric.market_pk),
        ts=metric.updated_at,
        last_price_dollars=cents_to_dollars(metric.last_price_cents),
        yes_bid_dollars=yes_bid,
        yes_ask_dollars=yes_ask,
        no_bid_dollars=cents_to_dollars(metric.no_bid_cents),
        no_ask_dollars=cents_to_dollars(metric.no_ask_cents),
        volume_24h_fp=contracts_to_decimal(metric.volume_24h_contracts),
        open_interest_fp=contracts_to_decimal(metric.open_interest_contracts),
        liquidity_dollars=cents_to_dollars(metric.liquidity_cents),
        source="market_metrics",
        source_key=str(metric.market_pk),
    )


def decimal_probability(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def display_price(point: QuotePoint) -> float | None:
    if point.last_price_dollars is not None:
        return decimal_probability(point.last_price_dollars)
    if point.yes_bid_dollars is not None and point.yes_ask_dollars is not None:
        return decimal_probability(
            (point.yes_bid_dollars + point.yes_ask_dollars) / Decimal("2")
        )
    if point.yes_bid_dollars is not None:
        return decimal_probability(point.yes_bid_dollars)
    if point.yes_ask_dollars is not None:
        return decimal_probability(point.yes_ask_dollars)
    return None


def quote_payload(point: QuotePoint) -> dict[str, Any]:
    return {
        "ts": point.ts.isoformat() if point.ts else None,
        "market_pk": point.market_pk,
        "yes_bid": decimal_probability(point.yes_bid_dollars),
        "yes_ask": decimal_probability(point.yes_ask_dollars),
        "last_price": decimal_probability(point.last_price_dollars),
        "volume": float(point.volume_fp) if point.volume_fp is not None else None,
        "volume_24h": (
            float(point.volume_24h_fp) if point.volume_24h_fp is not None else None
        ),
        "open_interest": (
            float(point.open_interest_fp)
            if point.open_interest_fp is not None
            else None
        ),
        "source": point.source,
        "source_key": point.source_key,
        "interval_sec": point.interval_sec,
        "trade_count": point.trade_count,
        "quote_count": point.quote_count,
    }


def latest_metric_values(
    db: Session,
    market_pks: list[int],
) -> dict[int, dict[str, float | None]]:
    if not market_pks:
        return {}
    rows = (
        db.query(MarketMetric)
        .filter(MarketMetric.market_pk.in_(market_pks))
        .all()
    )
    values: dict[int, dict[str, float | None]] = {}
    for metric in rows:
        point = metric_point(metric)
        values[int(metric.market_pk)] = {
            "last_price": display_price(point),
            "volume_24h": (
                float(point.volume_24h_fp)
                if point.volume_24h_fp is not None
                else None
            ),
            "volume_total": None,
            "yes_bid": decimal_probability(point.yes_bid_dollars),
            "yes_ask": decimal_probability(point.yes_ask_dollars),
            "open_interest": (
                float(point.open_interest_fp)
                if point.open_interest_fp is not None
                else None
            ),
            "liquidity": (
                float(point.liquidity_dollars)
                if point.liquidity_dollars is not None
                else None
            ),
            "ts": point.ts.isoformat() if point.ts else None,
            "source": point.source,
        }
    return values


def latest_history_points(
    db: Session,
    market_pks: list[int],
    *,
    limit_per_market: int = 1,
) -> dict[int, list[QuotePoint]]:
    if not market_pks:
        return {}

    out: dict[int, list[QuotePoint]] = {int(pk): [] for pk in market_pks}
    for pk in market_pks:
        rows = (
            db.query(MarketPriceHistory)
            .filter(MarketPriceHistory.market_pk == pk)
            .order_by(
                MarketPriceHistory.bucket_start.desc(),
                MarketPriceHistory.interval_sec.asc(),
            )
            .limit(max(1, limit_per_market))
            .all()
        )
        out[int(pk)] = [history_point(row) for row in rows]
    return out


def history_points_for_market(
    db: Session,
    market_pk: int,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 3000,
    newest_first: bool = False,
) -> list[QuotePoint]:
    q = db.query(MarketPriceHistory).filter(MarketPriceHistory.market_pk == market_pk)
    if start is not None:
        q = q.filter(MarketPriceHistory.bucket_start >= start)
    if end is not None:
        q = q.filter(MarketPriceHistory.bucket_start <= end)
    if newest_first:
        q = q.order_by(
            MarketPriceHistory.bucket_start.desc(),
            MarketPriceHistory.interval_sec.asc(),
        )
    else:
        q = q.order_by(
            MarketPriceHistory.bucket_start.asc(),
            MarketPriceHistory.interval_sec.asc(),
        )
    rows = q.limit(max(1, limit)).all()
    return [history_point(row) for row in rows]


def sibling_history_payloads(
    db: Session,
    market: Market,
    *,
    start: datetime,
    end: datetime,
    sibling_limit: int = 25,
    row_limit: int = 3000,
) -> list[dict[str, Any]]:
    if not market.event_id:
        return []
    sibling_pks = [
        int(pk)
        for (pk,) in (
            db.query(Market.id)
            .filter(Market.event_id == market.event_id)
            .filter(Market.id != market.id)
            .limit(max(1, sibling_limit))
            .all()
        )
    ]
    if not sibling_pks:
        return []
    rows = (
        db.query(MarketPriceHistory)
        .filter(MarketPriceHistory.market_pk.in_(sibling_pks))
        .filter(MarketPriceHistory.bucket_start >= start)
        .filter(MarketPriceHistory.bucket_start <= end)
        .order_by(
            MarketPriceHistory.bucket_start.asc(),
            desc(MarketPriceHistory.interval_sec),
            MarketPriceHistory.market_pk.asc(),
        )
        .limit(max(1, row_limit))
        .all()
    )
    return [quote_payload(history_point(row)) for row in rows]
