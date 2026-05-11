"""Compact chart-history writes for market price displays.

Raw trades and snapshots are operational evidence tables; retention can prune
or sample them. This module maintains a small bucketed read model that charts
can use after raw rows age out.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import MarketPriceHistory

DEFAULT_CHART_HISTORY_INTERVAL_SEC = int(
    os.getenv("KALSHI_CHART_HISTORY_INTERVAL_SEC", "300")
)
_QUOTE_MIN_INTERVAL_SEC = float(
    os.getenv("KALSHI_CHART_HISTORY_QUOTE_MIN_INTERVAL_SEC", "60")
)
_QUOTE_CACHE_MAX_AGE_BUCKETS = max(
    1,
    int(os.getenv("KALSHI_CHART_HISTORY_QUOTE_CACHE_MAX_AGE_BUCKETS", "3")),
)
_QUOTE_CACHE_PRUNE_INTERVAL_SEC = max(
    1.0,
    float(os.getenv("KALSHI_CHART_HISTORY_QUOTE_CACHE_PRUNE_INTERVAL_SEC", "300")),
)
_QUOTE_CACHE_MAX_ENTRIES = max(
    1000,
    int(os.getenv("KALSHI_CHART_HISTORY_QUOTE_CACHE_MAX_ENTRIES", "200000")),
)
_PRICE_SOURCE_RANK = {
    None: 0,
    "midpoint": 1,
    "last_price": 2,
    "trade": 3,
}
_last_quote_write_by_market_bucket: dict[tuple[int, int, datetime], float] = {}
_last_quote_cache_pruned_at = 0.0


def chart_history_enabled() -> bool:
    return os.getenv("KALSHI_CHART_HISTORY_ENABLED", "true").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def should_write_quote_history(
    market_pk: int,
    event_ts: datetime,
    *,
    interval_sec: int = DEFAULT_CHART_HISTORY_INTERVAL_SEC,
    now_m: float | None = None,
) -> bool:
    if _QUOTE_MIN_INTERVAL_SEC <= 0:
        return True
    bucket = bucket_start(event_ts, interval_sec=interval_sec)
    _prune_quote_write_cache(
        current_bucket=bucket,
        current_m=time.monotonic() if now_m is None else now_m,
    )
    key = (int(market_pk), int(interval_sec), bucket)
    current = time.monotonic() if now_m is None else now_m
    previous = _last_quote_write_by_market_bucket.get(key)
    if previous is not None and current - previous < _QUOTE_MIN_INTERVAL_SEC:
        return False
    _last_quote_write_by_market_bucket[key] = current
    return True


def _prune_quote_write_cache(
    *,
    current_bucket: datetime,
    current_m: float,
) -> None:
    """Keep the per-process quote throttle bounded in long-running workers."""
    global _last_quote_cache_pruned_at

    cache_size = len(_last_quote_write_by_market_bucket)
    if cache_size == 0:
        _last_quote_cache_pruned_at = current_m
        return

    if (
        cache_size <= _QUOTE_CACHE_MAX_ENTRIES
        and current_m - _last_quote_cache_pruned_at
        < _QUOTE_CACHE_PRUNE_INTERVAL_SEC
    ):
        return

    current_epoch = current_bucket.timestamp()
    stale_keys = [
        key
        for key in _last_quote_write_by_market_bucket
        if key[2].timestamp()
        < current_epoch - (int(key[1]) * _QUOTE_CACHE_MAX_AGE_BUCKETS)
    ]
    for key in stale_keys:
        _last_quote_write_by_market_bucket.pop(key, None)

    overflow = len(_last_quote_write_by_market_bucket) - _QUOTE_CACHE_MAX_ENTRIES
    if overflow > 0:
        oldest_keys = sorted(
            _last_quote_write_by_market_bucket,
            key=lambda key: key[2],
        )[:overflow]
        for key in oldest_keys:
            _last_quote_write_by_market_bucket.pop(key, None)

    _last_quote_cache_pruned_at = current_m


def bucket_start(
    ts: datetime,
    *,
    interval_sec: int = DEFAULT_CHART_HISTORY_INTERVAL_SEC,
) -> datetime:
    interval_sec = max(1, int(interval_sec))
    ts = _as_utc(ts)
    bucket_epoch = int(ts.timestamp()) // interval_sec * interval_sec
    return datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)


def quote_history_row(
    *,
    market_pk: int,
    event_ts: datetime,
    last_price_dollars: Decimal | None,
    yes_bid_dollars: Decimal | None,
    yes_ask_dollars: Decimal | None,
    volume_24h_fp: Decimal | None = None,
    open_interest_fp: Decimal | None = None,
    interval_sec: int = DEFAULT_CHART_HISTORY_INTERVAL_SEC,
) -> dict[str, Any] | None:
    if not chart_history_enabled():
        return None

    event_ts = _as_utc(event_ts)
    price, source = _quote_display_price(
        last_price_dollars=last_price_dollars,
        yes_bid_dollars=yes_bid_dollars,
        yes_ask_dollars=yes_ask_dollars,
    )
    if price is None and yes_bid_dollars is None and yes_ask_dollars is None:
        return None

    return {
        "market_pk": int(market_pk),
        "interval_sec": int(interval_sec),
        "bucket_start": bucket_start(event_ts, interval_sec=interval_sec),
        "open_price_dollars": price,
        "high_price_dollars": price,
        "low_price_dollars": price,
        "close_price_dollars": price,
        "close_price_source": source,
        "close_price_source_rank": _PRICE_SOURCE_RANK[source],
        "first_price_ts": event_ts if price is not None else None,
        "last_price_ts": event_ts if price is not None else None,
        "open_yes_bid_dollars": yes_bid_dollars,
        "high_yes_bid_dollars": yes_bid_dollars,
        "low_yes_bid_dollars": yes_bid_dollars,
        "close_yes_bid_dollars": yes_bid_dollars,
        "open_yes_ask_dollars": yes_ask_dollars,
        "high_yes_ask_dollars": yes_ask_dollars,
        "low_yes_ask_dollars": yes_ask_dollars,
        "close_yes_ask_dollars": yes_ask_dollars,
        "close_volume_24h_fp": volume_24h_fp,
        "close_open_interest_fp": open_interest_fp,
        "first_quote_ts": event_ts,
        "last_quote_ts": event_ts,
        "trade_count": 0,
        "trade_volume_contracts": Decimal("0"),
        "quote_count": 1,
    }


def trade_history_row(
    *,
    market_pk: int,
    trade_ts: datetime,
    yes_price_dollars: Decimal | None,
    count_fp: Decimal | None = None,
    interval_sec: int = DEFAULT_CHART_HISTORY_INTERVAL_SEC,
) -> dict[str, Any] | None:
    if not chart_history_enabled() or yes_price_dollars is None:
        return None

    trade_ts = _as_utc(trade_ts)
    count = count_fp or Decimal("0")
    return {
        "market_pk": int(market_pk),
        "interval_sec": int(interval_sec),
        "bucket_start": bucket_start(trade_ts, interval_sec=interval_sec),
        "open_price_dollars": yes_price_dollars,
        "high_price_dollars": yes_price_dollars,
        "low_price_dollars": yes_price_dollars,
        "close_price_dollars": yes_price_dollars,
        "close_price_source": "trade",
        "close_price_source_rank": _PRICE_SOURCE_RANK["trade"],
        "first_price_ts": trade_ts,
        "last_price_ts": trade_ts,
        "open_yes_bid_dollars": None,
        "high_yes_bid_dollars": None,
        "low_yes_bid_dollars": None,
        "close_yes_bid_dollars": None,
        "open_yes_ask_dollars": None,
        "high_yes_ask_dollars": None,
        "low_yes_ask_dollars": None,
        "close_yes_ask_dollars": None,
        "close_volume_24h_fp": None,
        "close_open_interest_fp": None,
        "first_quote_ts": None,
        "last_quote_ts": None,
        "trade_count": 1,
        "trade_volume_contracts": count,
        "quote_count": 0,
    }


def bulk_upsert_chart_history(db: Session, rows: list[dict[str, Any]]) -> None:
    rows = _coalesce_history_rows([row for row in rows if row])
    if not rows:
        return

    stmt = pg_insert(MarketPriceHistory).values(rows)
    excluded = stmt.excluded
    close_price_is_better = and_(
        excluded.close_price_dollars.isnot(None),
        or_(
            MarketPriceHistory.close_price_dollars.is_(None),
            excluded.close_price_source_rank
            > func.coalesce(MarketPriceHistory.close_price_source_rank, 0),
            and_(
                excluded.close_price_source_rank
                == func.coalesce(MarketPriceHistory.close_price_source_rank, 0),
                or_(
                    MarketPriceHistory.last_price_ts.is_(None),
                    excluded.last_price_ts >= MarketPriceHistory.last_price_ts,
                ),
            ),
        )
    )
    quote_is_newer = and_(
        excluded.last_quote_ts.isnot(None),
        or_(
            MarketPriceHistory.last_quote_ts.is_(None),
            excluded.last_quote_ts >= MarketPriceHistory.last_quote_ts,
        ),
    )

    stmt = stmt.on_conflict_do_update(
        index_elements=["market_pk", "interval_sec", "bucket_start"],
        set_={
            "open_price_dollars": case(
                (
                    MarketPriceHistory.first_price_ts.is_(None),
                    excluded.open_price_dollars,
                ),
                (
                    excluded.first_price_ts < MarketPriceHistory.first_price_ts,
                    excluded.open_price_dollars,
                ),
                else_=MarketPriceHistory.open_price_dollars,
            ),
            "high_price_dollars": _greatest_nullable(
                MarketPriceHistory.high_price_dollars,
                excluded.high_price_dollars,
            ),
            "low_price_dollars": _least_nullable(
                MarketPriceHistory.low_price_dollars,
                excluded.low_price_dollars,
            ),
            "close_price_dollars": case(
                (
                    close_price_is_better,
                    excluded.close_price_dollars,
                ),
                else_=MarketPriceHistory.close_price_dollars,
            ),
            "close_price_source": case(
                (
                    close_price_is_better,
                    excluded.close_price_source,
                ),
                else_=MarketPriceHistory.close_price_source,
            ),
            "close_price_source_rank": case(
                (
                    close_price_is_better,
                    excluded.close_price_source_rank,
                ),
                else_=MarketPriceHistory.close_price_source_rank,
            ),
            "first_price_ts": _least_nullable(
                MarketPriceHistory.first_price_ts,
                excluded.first_price_ts,
            ),
            "last_price_ts": _greatest_nullable(
                MarketPriceHistory.last_price_ts,
                excluded.last_price_ts,
            ),
            "open_yes_bid_dollars": case(
                (
                    MarketPriceHistory.first_quote_ts.is_(None),
                    excluded.open_yes_bid_dollars,
                ),
                (
                    excluded.first_quote_ts < MarketPriceHistory.first_quote_ts,
                    excluded.open_yes_bid_dollars,
                ),
                else_=MarketPriceHistory.open_yes_bid_dollars,
            ),
            "high_yes_bid_dollars": _greatest_nullable(
                MarketPriceHistory.high_yes_bid_dollars,
                excluded.high_yes_bid_dollars,
            ),
            "low_yes_bid_dollars": _least_nullable(
                MarketPriceHistory.low_yes_bid_dollars,
                excluded.low_yes_bid_dollars,
            ),
            "close_yes_bid_dollars": case(
                (
                    quote_is_newer,
                    func.coalesce(
                        excluded.close_yes_bid_dollars,
                        MarketPriceHistory.close_yes_bid_dollars,
                    ),
                ),
                else_=MarketPriceHistory.close_yes_bid_dollars,
            ),
            "open_yes_ask_dollars": case(
                (
                    MarketPriceHistory.first_quote_ts.is_(None),
                    excluded.open_yes_ask_dollars,
                ),
                (
                    excluded.first_quote_ts < MarketPriceHistory.first_quote_ts,
                    excluded.open_yes_ask_dollars,
                ),
                else_=MarketPriceHistory.open_yes_ask_dollars,
            ),
            "high_yes_ask_dollars": _greatest_nullable(
                MarketPriceHistory.high_yes_ask_dollars,
                excluded.high_yes_ask_dollars,
            ),
            "low_yes_ask_dollars": _least_nullable(
                MarketPriceHistory.low_yes_ask_dollars,
                excluded.low_yes_ask_dollars,
            ),
            "close_yes_ask_dollars": case(
                (
                    quote_is_newer,
                    func.coalesce(
                        excluded.close_yes_ask_dollars,
                        MarketPriceHistory.close_yes_ask_dollars,
                    ),
                ),
                else_=MarketPriceHistory.close_yes_ask_dollars,
            ),
            "close_volume_24h_fp": case(
                (
                    quote_is_newer,
                    func.coalesce(
                        excluded.close_volume_24h_fp,
                        MarketPriceHistory.close_volume_24h_fp,
                    ),
                ),
                else_=MarketPriceHistory.close_volume_24h_fp,
            ),
            "close_open_interest_fp": case(
                (
                    quote_is_newer,
                    func.coalesce(
                        excluded.close_open_interest_fp,
                        MarketPriceHistory.close_open_interest_fp,
                    ),
                ),
                else_=MarketPriceHistory.close_open_interest_fp,
            ),
            "first_quote_ts": _least_nullable(
                MarketPriceHistory.first_quote_ts,
                excluded.first_quote_ts,
            ),
            "last_quote_ts": _greatest_nullable(
                MarketPriceHistory.last_quote_ts,
                excluded.last_quote_ts,
            ),
            "trade_count": MarketPriceHistory.trade_count + excluded.trade_count,
            "trade_volume_contracts": (
                MarketPriceHistory.trade_volume_contracts
                + excluded.trade_volume_contracts
            ),
            "quote_count": MarketPriceHistory.quote_count + excluded.quote_count,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)


def _coalesce_history_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[int, int, datetime], dict[str, Any]] = {}
    for row in rows:
        key = (
            int(row["market_pk"]),
            int(row["interval_sec"]),
            row["bucket_start"],
        )
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = dict(row)
        else:
            _merge_history_row(existing, row)
    return list(by_key.values())


def _merge_history_row(existing: dict[str, Any], row: dict[str, Any]) -> None:
    if _candidate_has_earlier_ts(row, existing, "first_price_ts"):
        existing["open_price_dollars"] = row.get("open_price_dollars")
    existing["high_price_dollars"] = _max_nullable(
        existing.get("high_price_dollars"),
        row.get("high_price_dollars"),
    )
    existing["low_price_dollars"] = _min_nullable(
        existing.get("low_price_dollars"),
        row.get("low_price_dollars"),
    )
    if _price_row_wins(row, existing):
        existing["close_price_dollars"] = row.get("close_price_dollars")
        existing["close_price_source"] = row.get("close_price_source")
        existing["close_price_source_rank"] = row.get("close_price_source_rank") or 0
    existing["first_price_ts"] = _min_nullable(
        existing.get("first_price_ts"),
        row.get("first_price_ts"),
    )
    existing["last_price_ts"] = _max_nullable(
        existing.get("last_price_ts"),
        row.get("last_price_ts"),
    )

    if _candidate_has_earlier_ts(row, existing, "first_quote_ts"):
        existing["open_yes_bid_dollars"] = row.get("open_yes_bid_dollars")
        existing["open_yes_ask_dollars"] = row.get("open_yes_ask_dollars")
    existing["high_yes_bid_dollars"] = _max_nullable(
        existing.get("high_yes_bid_dollars"),
        row.get("high_yes_bid_dollars"),
    )
    existing["low_yes_bid_dollars"] = _min_nullable(
        existing.get("low_yes_bid_dollars"),
        row.get("low_yes_bid_dollars"),
    )
    existing["high_yes_ask_dollars"] = _max_nullable(
        existing.get("high_yes_ask_dollars"),
        row.get("high_yes_ask_dollars"),
    )
    existing["low_yes_ask_dollars"] = _min_nullable(
        existing.get("low_yes_ask_dollars"),
        row.get("low_yes_ask_dollars"),
    )
    if _quote_row_is_newer(row, existing):
        for field in (
            "close_yes_bid_dollars",
            "close_yes_ask_dollars",
            "close_volume_24h_fp",
            "close_open_interest_fp",
        ):
            if row.get(field) is not None:
                existing[field] = row.get(field)
    existing["first_quote_ts"] = _min_nullable(
        existing.get("first_quote_ts"),
        row.get("first_quote_ts"),
    )
    existing["last_quote_ts"] = _max_nullable(
        existing.get("last_quote_ts"),
        row.get("last_quote_ts"),
    )

    existing["trade_count"] = int(existing.get("trade_count") or 0) + int(
        row.get("trade_count") or 0
    )
    existing["trade_volume_contracts"] = (
        existing.get("trade_volume_contracts") or Decimal("0")
    ) + (row.get("trade_volume_contracts") or Decimal("0"))
    existing["quote_count"] = int(existing.get("quote_count") or 0) + int(
        row.get("quote_count") or 0
    )


def _candidate_has_earlier_ts(
    candidate: dict[str, Any],
    existing: dict[str, Any],
    ts_field: str,
) -> bool:
    candidate_ts = candidate.get(ts_field)
    if candidate_ts is None:
        return False
    existing_ts = existing.get(ts_field)
    return existing_ts is None or candidate_ts < existing_ts


def _price_row_wins(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    if candidate.get("close_price_dollars") is None:
        return False
    if existing.get("close_price_dollars") is None:
        return True
    candidate_rank = int(candidate.get("close_price_source_rank") or 0)
    existing_rank = int(existing.get("close_price_source_rank") or 0)
    if candidate_rank != existing_rank:
        return candidate_rank > existing_rank
    candidate_ts = candidate.get("last_price_ts")
    existing_ts = existing.get("last_price_ts")
    return candidate_ts is not None and (
        existing_ts is None or candidate_ts >= existing_ts
    )


def _quote_row_is_newer(candidate: dict[str, Any], existing: dict[str, Any]) -> bool:
    candidate_ts = candidate.get("last_quote_ts")
    if candidate_ts is None:
        return False
    existing_ts = existing.get("last_quote_ts")
    return existing_ts is None or candidate_ts >= existing_ts


def _max_nullable(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _min_nullable(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def upsert_chart_history(db: Session, row: dict[str, Any] | None) -> None:
    if row:
        bulk_upsert_chart_history(db, [row])


def _quote_display_price(
    *,
    last_price_dollars: Decimal | None,
    yes_bid_dollars: Decimal | None,
    yes_ask_dollars: Decimal | None,
) -> tuple[Decimal | None, str | None]:
    if last_price_dollars is not None:
        return last_price_dollars, "last_price"
    if yes_bid_dollars is not None and yes_ask_dollars is not None:
        return (yes_bid_dollars + yes_ask_dollars) / Decimal("2"), "midpoint"
    return None, None


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _greatest_nullable(left, right):
    return case(
        (left.is_(None), right),
        (right.is_(None), left),
        else_=func.greatest(left, right),
    )


def _least_nullable(left, right):
    return case(
        (left.is_(None), right),
        (right.is_(None), left),
        else_=func.least(left, right),
    )
