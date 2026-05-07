"""Backfill compact chart history from retained snapshots and trades.

Dry-run is the default. Pass ``--execute`` to populate
``market_price_history``. The defaults are intentionally bounded so this can
be run safely on production before widening the market set.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.db.models import Market, MarketMetric, MarketSnapshot, Trade
from app.db.session import SessionLocal
from app.services.market_price_history import (
    DEFAULT_CHART_HISTORY_INTERVAL_SEC,
    bulk_upsert_chart_history,
    quote_history_row,
    trade_history_row,
)
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
)


ACTIVE_STATUSES = ("active", "open", "unknown")


def _cutoff(since_days: int | None) -> datetime | None:
    if since_days is None or since_days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=since_days)


def _candidate_markets(
    db: Session,
    *,
    market_ids: list[str],
    active_only: bool,
    priors: list[str],
    max_markets: int,
    offset: int,
) -> list[Market]:
    q = db.query(Market)
    if market_ids:
        return q.filter(Market.market_id.in_(market_ids)).order_by(Market.id.asc()).all()

    q = q.outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
    if active_only:
        q = q.filter(Market.status.in_(ACTIVE_STATUSES))
    if priors:
        q = q.filter(Market.manipulability_prior.in_(priors))
    return (
        q.order_by(
            desc(MarketMetric.volume_24h_contracts).nullslast(),
            desc(MarketMetric.trade_count),
            desc(MarketMetric.updated_at).nullslast(),
            Market.id.asc(),
        )
        .offset(max(0, offset))
        .limit(max(1, max_markets))
        .all()
    )


def _snapshot_rows(
    db: Session,
    market_pk: int,
    *,
    cutoff: datetime | None,
    limit: int,
) -> list[MarketSnapshot]:
    q = db.query(MarketSnapshot).filter(MarketSnapshot.market_pk == market_pk)
    if cutoff is not None:
        q = q.filter(MarketSnapshot.ts >= cutoff)
    rows = (
        q.order_by(MarketSnapshot.ts.desc(), MarketSnapshot.id.desc())
        .limit(max(0, limit))
        .all()
    )
    return list(reversed(rows))


def _trade_rows(
    db: Session,
    market_pk: int,
    *,
    cutoff: datetime | None,
    limit: int,
) -> list[Trade]:
    q = db.query(Trade).filter(Trade.market_pk == market_pk)
    if cutoff is not None:
        q = q.filter(Trade.ts >= cutoff)
    rows = q.order_by(Trade.ts.desc(), Trade.id.desc()).limit(max(0, limit)).all()
    return list(reversed(rows))


def _history_rows_for_market(
    db: Session,
    market: Market,
    *,
    cutoff: datetime | None,
    interval_sec: int,
    max_snapshots: int,
    max_trades: int,
) -> tuple[list[dict[str, Any]], int, int]:
    rows: list[dict[str, Any]] = []
    snapshots = _snapshot_rows(
        db,
        int(market.id),
        cutoff=cutoff,
        limit=max_snapshots,
    )
    for snapshot in snapshots:
        row = quote_history_row(
            market_pk=market.id,
            event_ts=snapshot.ts,
            last_price_dollars=snapshot.last_price_dollars,
            yes_bid_dollars=snapshot.yes_bid_dollars,
            yes_ask_dollars=snapshot.yes_ask_dollars,
            volume_24h_fp=snapshot.volume_24h_fp,
            open_interest_fp=snapshot.open_interest_fp,
            interval_sec=interval_sec,
        )
        if row:
            rows.append(row)

    trades = _trade_rows(
        db,
        int(market.id),
        cutoff=cutoff,
        limit=max_trades,
    )
    for trade in trades:
        row = trade_history_row(
            market_pk=market.id,
            trade_ts=trade.ts,
            yes_price_dollars=trade.yes_price_dollars,
            count_fp=trade.count_fp,
            interval_sec=interval_sec,
        )
        if row:
            rows.append(row)

    return rows, len(snapshots), len(trades)


def backfill_market_price_history(
    db: Session,
    *,
    market_ids: list[str],
    active_only: bool,
    priors: list[str],
    max_markets: int,
    offset: int,
    since_days: int | None,
    interval_sec: int,
    max_snapshots_per_market: int,
    max_trades_per_market: int,
    execute: bool,
) -> dict[str, Any]:
    cutoff = _cutoff(since_days)
    markets = _candidate_markets(
        db,
        market_ids=market_ids,
        active_only=active_only,
        priors=priors,
        max_markets=max_markets,
        offset=offset,
    )
    result: dict[str, Any] = {
        "dry_run": not execute,
        "markets": len(markets),
        "snapshot_rows_read": 0,
        "trade_rows_read": 0,
        "history_rows_built": 0,
        "history_buckets_touched": 0,
        "interval_sec": interval_sec,
        "since_days": since_days,
    }
    for market in markets:
        rows, snapshot_count, trade_count = _history_rows_for_market(
            db,
            market,
            cutoff=cutoff,
            interval_sec=interval_sec,
            max_snapshots=max_snapshots_per_market,
            max_trades=max_trades_per_market,
        )
        result["snapshot_rows_read"] += snapshot_count
        result["trade_rows_read"] += trade_count
        result["history_rows_built"] += len(rows)
        result["history_buckets_touched"] += len(
            {
                (
                    int(row["market_pk"]),
                    int(row["interval_sec"]),
                    row["bucket_start"],
                )
                for row in rows
            }
        )
        if execute and rows:
            bulk_upsert_chart_history(db, rows)
            db.commit()
    if not execute:
        db.rollback()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-id", action="append", default=[])
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--prior", action="append", default=[])
    parser.add_argument("--max-markets", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--since-days", type=int, default=14)
    parser.add_argument(
        "--interval-sec",
        type=int,
        default=DEFAULT_CHART_HISTORY_INTERVAL_SEC,
    )
    parser.add_argument("--max-snapshots-per-market", type=int, default=2000)
    parser.add_argument("--max-trades-per-market", type=int, default=2000)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    run_id = new_run_id("market-price-history")
    mark_pipeline_start(
        "chart_history_backfill",
        detail="Starting market_price_history backfill.",
        run_id=run_id,
        metadata={
            "dry_run": not args.execute,
            "max_markets": args.max_markets,
            "active_only": args.active_only,
        },
    )
    db = SessionLocal()
    try:
        result = backfill_market_price_history(
            db,
            market_ids=list(args.market_id or []),
            active_only=bool(args.active_only),
            priors=list(args.prior or []),
            max_markets=args.max_markets,
            offset=args.offset,
            since_days=args.since_days,
            interval_sec=max(1, args.interval_sec),
            max_snapshots_per_market=max(0, args.max_snapshots_per_market),
            max_trades_per_market=max(0, args.max_trades_per_market),
            execute=bool(args.execute),
        )
        mark_pipeline_success(
            "chart_history_backfill",
            detail=(
                f"Built {result['history_rows_built']} chart-history source rows "
                f"for {result['markets']} markets."
            ),
            run_id=run_id,
            count=int(result["history_buckets_touched"]),
            metadata=result,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        db.rollback()
        mark_pipeline_error(
            "chart_history_backfill",
            exc,
            detail="market_price_history backfill failed.",
            run_id=run_id,
        )
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
