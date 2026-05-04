"""Rebuild compact ``market_metrics`` counts from existing raw tables.

This is the one-time bridge that lets dashboard list/overview endpoints read
from the projection table instead of repeatedly grouping raw trades/anomalies.
It preserves existing storage-tier/retention fields on conflict.
"""

from __future__ import annotations

import argparse
import json
from datetime import timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import case, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Anomaly, Market, MarketMetric, MarketSnapshot, Trade
from app.db.session import SessionLocal
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
)
from app.services.surveillance_scores import (
    evidence_score_0_100,
    prior_rank,
    urgency_score_0_100,
)


def _cents(value: Decimal | None) -> int | None:
    if value is None:
        return None
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _contracts(value: Decimal | None) -> int | None:
    if value is None:
        return None
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _market_batches(
    db: Session,
    *,
    batch_size: int,
    max_markets: int | None,
    market_id: str | None,
) -> list[list[Market]]:
    q = db.query(Market).order_by(Market.id.asc())
    if market_id:
        q = q.filter(Market.market_id == market_id)
    if max_markets is not None:
        q = q.limit(max_markets)
    markets = q.all()
    return [markets[i : i + batch_size] for i in range(0, len(markets), batch_size)]


def _latest_snapshots(db: Session, market_pks: list[int]) -> dict[int, MarketSnapshot]:
    if not market_pks:
        return {}
    ids = [
        int(row.id)
        for row in db.execute(
            text(
                """
                SELECT picked.id
                FROM unnest(CAST(:market_pks AS integer[])) AS scope(market_pk)
                JOIN LATERAL (
                    SELECT ms.id
                    FROM market_snapshots ms
                    WHERE ms.market_pk = scope.market_pk
                    ORDER BY ms.ts DESC, ms.id DESC
                    LIMIT 1
                ) AS picked ON TRUE
                """
            ),
            {"market_pks": market_pks},
        ).all()
    ]

    if not ids:
        return {}

    return {
        int(row.market_pk): row
        for row in db.query(MarketSnapshot).filter(MarketSnapshot.id.in_(ids)).all()
    }


def _trade_stats(db: Session, market_pks: list[int]) -> dict[int, dict[str, Any]]:
    rows = (
        db.query(
            Trade.market_pk,
            func.count(Trade.id).label("trade_count"),
            func.max(Trade.ts).label("last_trade_ts"),
        )
        .filter(Trade.market_pk.in_(market_pks))
        .group_by(Trade.market_pk)
        .all()
    )
    return {
        int(row.market_pk): {
            "trade_count": int(row.trade_count or 0),
            "last_trade_ts": row.last_trade_ts,
        }
        for row in rows
    }


def _anomaly_stats(db: Session, market_pks: list[int]) -> dict[int, dict[str, Any]]:
    high_case = case((Anomaly.severity.in_(("high", "critical")), 1), else_=0)
    rows = (
        db.query(
            Anomaly.market_pk,
            func.count(Anomaly.id).label("anomaly_count"),
            func.sum(high_case).label("high_anomaly_count"),
            func.max(Anomaly.created_at).label("last_anomaly_ts"),
        )
        .filter(Anomaly.market_pk.in_(market_pks))
        .group_by(Anomaly.market_pk)
        .all()
    )
    return {
        int(row.market_pk): {
            "anomaly_count": int(row.anomaly_count or 0),
            "high_anomaly_count": int(row.high_anomaly_count or 0),
            "last_anomaly_ts": row.last_anomaly_ts,
        }
        for row in rows
    }


def _values_for_market(
    market: Market,
    *,
    snapshot: MarketSnapshot | None,
    trade_stats: dict[str, Any],
    anomaly_stats: dict[str, Any],
) -> dict[str, Any]:
    anomaly_count = int(anomaly_stats.get("anomaly_count") or 0)
    pr = prior_rank(market.manipulability_prior)
    latest_ts = snapshot.ts if snapshot is not None else None
    if latest_ts is not None and latest_ts.tzinfo is None:
        latest_ts = latest_ts.replace(tzinfo=timezone.utc)
    return {
        "market_pk": market.id,
        "latest_snapshot_id": snapshot.id if snapshot is not None else None,
        "latest_snapshot_ts": latest_ts,
        "last_price_cents": _cents(snapshot.last_price_dollars)
        if snapshot is not None
        else None,
        "yes_bid_cents": _cents(snapshot.yes_bid_dollars)
        if snapshot is not None
        else None,
        "yes_ask_cents": _cents(snapshot.yes_ask_dollars)
        if snapshot is not None
        else None,
        "no_bid_cents": _cents(snapshot.no_bid_dollars) if snapshot is not None else None,
        "no_ask_cents": _cents(snapshot.no_ask_dollars) if snapshot is not None else None,
        "volume_24h_contracts": _contracts(snapshot.volume_24h_fp)
        if snapshot is not None
        else None,
        "open_interest_contracts": _contracts(snapshot.open_interest_fp)
        if snapshot is not None
        else None,
        "liquidity_cents": _cents(snapshot.liquidity_dollars)
        if snapshot is not None
        else None,
        "trade_count": int(trade_stats.get("trade_count") or 0),
        "last_trade_ts": trade_stats.get("last_trade_ts"),
        "anomaly_count": anomaly_count,
        "high_anomaly_count": int(anomaly_stats.get("high_anomaly_count") or 0),
        "last_anomaly_ts": anomaly_stats.get("last_anomaly_ts"),
        "urgency_score": urgency_score_0_100(prior_rank=pr, anomaly_count=anomaly_count),
        "evidence_score": evidence_score_0_100(anomaly_count=anomaly_count),
        "storage_tier": "observe_only",
        "retention_score": 0,
        "retention_reasons": [],
        "updated_at": func.now(),
    }


def backfill_market_metrics(
    db: Session,
    *,
    batch_size: int = 1000,
    max_markets: int | None = None,
    market_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    processed = 0
    upserted = 0
    for batch in _market_batches(
        db,
        batch_size=max(1, batch_size),
        max_markets=max_markets,
        market_id=market_id,
    ):
        market_pks = [int(market.id) for market in batch]
        snapshots = _latest_snapshots(db, market_pks)
        trades = _trade_stats(db, market_pks)
        anomalies = _anomaly_stats(db, market_pks)
        values = [
            _values_for_market(
                market,
                snapshot=snapshots.get(market.id),
                trade_stats=trades.get(market.id, {}),
                anomaly_stats=anomalies.get(market.id, {}),
            )
            for market in batch
        ]
        processed += len(values)
        if dry_run or not values:
            continue
        stmt = pg_insert(MarketMetric).values(values)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["market_pk"],
            set_={
                "latest_snapshot_id": excluded.latest_snapshot_id,
                "latest_snapshot_ts": excluded.latest_snapshot_ts,
                "last_price_cents": excluded.last_price_cents,
                "yes_bid_cents": excluded.yes_bid_cents,
                "yes_ask_cents": excluded.yes_ask_cents,
                "no_bid_cents": excluded.no_bid_cents,
                "no_ask_cents": excluded.no_ask_cents,
                "volume_24h_contracts": excluded.volume_24h_contracts,
                "open_interest_contracts": excluded.open_interest_contracts,
                "liquidity_cents": excluded.liquidity_cents,
                "trade_count": excluded.trade_count,
                "last_trade_ts": excluded.last_trade_ts,
                "anomaly_count": excluded.anomaly_count,
                "high_anomaly_count": excluded.high_anomaly_count,
                "last_anomaly_ts": excluded.last_anomaly_ts,
                "urgency_score": excluded.urgency_score,
                "evidence_score": excluded.evidence_score,
                "updated_at": func.now(),
            },
        )
        db.execute(stmt)
        db.commit()
        upserted += len(values)
    if dry_run:
        db.rollback()
    return {"processed": processed, "upserted": upserted, "dry_run": dry_run}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-markets", type=int, default=None)
    parser.add_argument("--market-id", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_id = new_run_id("market-metrics")
    mark_pipeline_start(
        "retention_projection",
        detail="Starting market_metrics backfill.",
        run_id=run_id,
    )
    db = SessionLocal()
    try:
        result = backfill_market_metrics(
            db,
            batch_size=args.batch_size,
            max_markets=args.max_markets,
            market_id=args.market_id,
            dry_run=args.dry_run,
        )
        mark_pipeline_success(
            "retention_projection",
            detail=(
                f"Backfilled {result['upserted']} market metric rows "
                f"from {result['processed']} markets."
            ),
            run_id=run_id,
            count=int(result["upserted"]),
            metadata=result,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        db.rollback()
        mark_pipeline_error(
            "retention_projection",
            exc,
            detail="market_metrics backfill failed.",
            run_id=run_id,
        )
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
