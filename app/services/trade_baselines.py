"""Materialized peer baselines for contextual trade scoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Market, Trade, TradeBaseline
from app.services.market_taxonomy import normalized_category_for_market
from app.services.trade_context import PeerBaseline, build_peer_baselines


@dataclass(frozen=True)
class BaselineScope:
    category: str
    subcategory: str
    liquidity_bucket: str = "all"
    time_to_close_bucket: str = "all"


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def trade_rows_for_baselines(
    db: Session,
    *,
    since: datetime,
    until: datetime,
    limit: int,
) -> list[dict]:
    rows = (
        db.query(Trade, Market)
        .join(Market, Market.id == Trade.market_pk)
        .filter(Trade.ts >= since)
        .filter(Trade.ts < until)
        .order_by(Trade.ts.asc(), Trade.id.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "market_pk": t.market_pk,
            "category": normalized_category_for_market(
                category=m.category,
                event_id=m.event_id,
                market_id=m.market_id,
                title=m.title,
            ),
            "subcategory": m.subcategory,
            "ts": t.ts.isoformat() if t.ts else None,
            "yes_price": float(t.yes_price_dollars)
            if t.yes_price_dollars is not None
            else None,
            "count": float(t.count_fp) if t.count_fp is not None else None,
        }
        for t, m in rows
        if m.category
    ]


def upsert_trade_baselines(
    db: Session,
    *,
    baselines: dict[tuple[str, str], PeerBaseline],
    window_start: datetime,
    window_end: datetime,
    scorer_version: int,
) -> int:
    rows = []
    for (category, subcategory), baseline in baselines.items():
        rows.append(
            {
                "category": category,
                "subcategory": subcategory,
                "liquidity_bucket": "all",
                "time_to_close_bucket": "all",
                "window_start": _aware(window_start),
                "window_end": _aware(window_end),
                "sample_size": baseline.sample_size,
                "count_p95": baseline.count_p95,
                "count_p99": baseline.count_p99,
                "abs_price_delta_p95": baseline.abs_price_delta_p95,
                "impact_p95": 0.0,
                "impact_p99": 0.0,
                "scorer_version": scorer_version,
            }
        )
    if not rows:
        return 0
    stmt = pg_insert(TradeBaseline).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_trade_baselines_scope_window_version",
        set_={
            "sample_size": stmt.excluded.sample_size,
            "count_p95": stmt.excluded.count_p95,
            "count_p99": stmt.excluded.count_p99,
            "abs_price_delta_p95": stmt.excluded.abs_price_delta_p95,
            "impact_p95": stmt.excluded.impact_p95,
            "impact_p99": stmt.excluded.impact_p99,
        },
    )
    db.execute(stmt)
    return len(rows)


def materialize_trade_baselines(
    db: Session,
    *,
    lookback_hours: int,
    max_trades: int,
    min_points: int,
    scorer_version: int,
    now: datetime | None = None,
) -> int:
    until = now or datetime.now(timezone.utc)
    since = until - timedelta(hours=lookback_hours)
    rows = trade_rows_for_baselines(db, since=since, until=until, limit=max_trades)
    baselines = build_peer_baselines(rows, min_points=min_points)
    return upsert_trade_baselines(
        db,
        baselines=baselines,
        window_start=since,
        window_end=until,
        scorer_version=scorer_version,
    )


def latest_baseline_for_market(
    db: Session,
    market: Market,
    *,
    scorer_version: int,
) -> PeerBaseline | None:
    category = normalized_category_for_market(
        category=market.category,
        event_id=market.event_id,
        market_id=market.market_id,
        title=market.title,
    )
    if not category:
        return None
    scopes: Iterable[BaselineScope] = (
        BaselineScope(category, market.subcategory or "*"),
        BaselineScope(category, "*"),
    )
    for scope in scopes:
        row = (
            db.query(TradeBaseline)
            .filter(TradeBaseline.category == scope.category)
            .filter(TradeBaseline.subcategory == scope.subcategory)
            .filter(TradeBaseline.liquidity_bucket == scope.liquidity_bucket)
            .filter(TradeBaseline.time_to_close_bucket == scope.time_to_close_bucket)
            .filter(TradeBaseline.scorer_version == scorer_version)
            .order_by(TradeBaseline.window_end.desc())
            .first()
        )
        if row is not None:
            return PeerBaseline(
                count_p95=float(row.count_p95),
                count_p99=float(row.count_p99),
                abs_price_delta_p95=float(row.abs_price_delta_p95),
                sample_size=int(row.sample_size),
            )
    return None
