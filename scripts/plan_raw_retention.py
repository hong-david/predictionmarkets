"""Print the disabled raw-table pruning strategy and optional read-only counts."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import exists, func, select, text
from sqlalchemy.orm import Session

from app.db.models import (
    Anomaly,
    Market,
    MarketMetric,
    MarketPriceHistory,
    MarketSnapshot,
    Trade,
    TradeFlag,
)
from app.db.session import SessionLocal
from app.services.market_lifecycle import ACTIVE_MARKET_STATUSES
from app.services.raw_retention_strategy import pruning_policy_payload


def _table_sizes(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT
                relname,
                pg_total_relation_size(relid) AS total_bytes,
                n_live_tup,
                n_dead_tup,
                last_vacuum,
                last_autovacuum
            FROM pg_stat_user_tables
            WHERE relname IN (
                'market_snapshots',
                'trades',
                'anomalies',
                'trade_flags',
                'market_metrics',
                'market_price_history'
            )
            ORDER BY pg_total_relation_size(relid) DESC
            """
        )
    ).mappings()
    return [dict(row) for row in rows]


def _closed_market_ids(
    db: Session,
    *,
    grace_days: int,
    max_markets: int,
) -> list[int]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
    rows = (
        db.query(Market.id)
        .filter(
            (Market.close_time <= cutoff)
            | (
                Market.status.notin_(tuple(ACTIVE_MARKET_STATUSES | {"unknown"}))
                & (Market.updated_at <= cutoff)
            )
        )
        .order_by(Market.id.asc())
        .limit(max(1, max_markets))
        .all()
    )
    return [int(row[0]) for row in rows]


def _count_scalar(db: Session, stmt) -> int:
    return int(db.execute(stmt).scalar_one() or 0)


def _candidate_estimates(
    db: Session,
    *,
    max_markets: int,
) -> dict[str, Any]:
    snapshot_market_ids = _closed_market_ids(db, grace_days=7, max_markets=max_markets)
    trade_market_ids = _closed_market_ids(db, grace_days=30, max_markets=max_markets)
    flag_market_ids = _closed_market_ids(db, grace_days=90, max_markets=max_markets)
    anomaly_cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    snapshot_candidates = 0
    latest_snapshot_ids = {
        int(row[0])
        for row in db.execute(
            select(MarketMetric.latest_snapshot_id).where(
                MarketMetric.latest_snapshot_id.isnot(None),
                MarketMetric.market_pk.in_(snapshot_market_ids or [-1]),
            )
        ).all()
    }
    if snapshot_market_ids:
        snapshot_candidates = _count_scalar(
            db,
            select(func.count())
            .select_from(MarketSnapshot)
            .where(MarketSnapshot.market_pk.in_(snapshot_market_ids))
            .where(
                ~exists(
                    select(Anomaly.id).where(
                        Anomaly.latest_snapshot_id == MarketSnapshot.id
                    )
                )
            )
            .where(
                exists(
                    select(MarketPriceHistory.market_pk).where(
                        MarketPriceHistory.market_pk == MarketSnapshot.market_pk,
                        MarketPriceHistory.interval_sec == 300,
                    )
                )
            ),
        )
        if latest_snapshot_ids:
            snapshot_candidates = max(0, snapshot_candidates - len(latest_snapshot_ids))

    trade_candidates = (
        _count_scalar(
            db,
            select(func.count())
            .select_from(Trade)
            .where(Trade.market_pk.in_(trade_market_ids)),
        )
        if trade_market_ids
        else 0
    )
    anomaly_candidates = _count_scalar(
        db,
        select(func.count())
        .select_from(Anomaly)
        .where(Anomaly.created_at < anomaly_cutoff)
        .where(Anomaly.severity.in_(("none", "low"))),
    )
    trade_flag_candidates = (
        _count_scalar(
            db,
            select(func.count())
            .select_from(TradeFlag)
            .where(TradeFlag.market_pk.in_(flag_market_ids)),
        )
        if flag_market_ids
        else 0
    )

    return {
        "dry_run_only": True,
        "max_closed_markets_scanned_per_table": max_markets,
        "market_snapshots_candidate_rows": snapshot_candidates,
        "trades_candidate_rows": trade_candidates,
        "anomalies_low_none_candidate_rows": anomaly_candidates,
        "trade_flags_candidate_rows": trade_flag_candidates,
        "market_metrics_candidate_rows": 0,
        "notes": [
            "These are planning estimates, not deletion counts.",
            "No delete SQL exists in this script.",
            "Snapshot estimate requires market_price_history coverage and excludes anomaly-referenced snapshots.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-candidate-estimates", action="store_true")
    parser.add_argument("--max-markets", type=int, default=1000)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        payload = pruning_policy_payload()
        payload["table_sizes"] = _table_sizes(db)
        if args.include_candidate_estimates:
            payload["candidate_estimates"] = _candidate_estimates(
                db,
                max_markets=max(1, args.max_markets),
            )
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    main()
