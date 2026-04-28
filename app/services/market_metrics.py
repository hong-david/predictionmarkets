"""Incremental read-model updates for the dashboard and retention policy."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import MarketMetric
from app.services.pipeline_heartbeat import record_pipeline_heartbeat
from app.services.retention import StorageDecision
from app.services.surveillance_scores import (
    evidence_score_0_100,
    prior_rank,
    urgency_score_0_100,
)

_RETENTION_PROJECTION_HEARTBEAT_INTERVAL_SEC = 60.0
_last_retention_projection_heartbeat = 0.0


def _cents(value: Decimal | None) -> int | None:
    if value is None:
        return None
    return int((value * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _contracts(value: Decimal | None) -> int | None:
    if value is None:
        return None
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _maybe_heartbeat_retention_projection(decision: StorageDecision) -> None:
    global _last_retention_projection_heartbeat
    now = time.monotonic()
    if now - _last_retention_projection_heartbeat < (
        _RETENTION_PROJECTION_HEARTBEAT_INTERVAL_SEC
    ):
        return
    _last_retention_projection_heartbeat = now
    record_pipeline_heartbeat(
        "retention_projection",
        detail=(
            f"MarketMetric projection active; latest storage tier {decision.tier} "
            f"with retention score {decision.score}."
        ),
        metadata={"storage_tier": decision.tier, "reasons": list(decision.reasons)},
    )


def upsert_quote_metrics(
    db: Session,
    *,
    market_pk: int,
    prior: str | None,
    latest_snapshot_id: int | None,
    latest_snapshot_ts: datetime | None,
    last_price_dollars: Decimal | None,
    yes_bid_dollars: Decimal | None,
    yes_ask_dollars: Decimal | None,
    no_bid_dollars: Decimal | None,
    no_ask_dollars: Decimal | None,
    volume_24h_fp: Decimal | None,
    open_interest_fp: Decimal | None,
    liquidity_dollars: Decimal | None,
    decision: StorageDecision,
) -> None:
    pr = prior_rank(prior)
    urgency = urgency_score_0_100(prior_rank=pr, anomaly_count=0)
    evidence = evidence_score_0_100(anomaly_count=0)
    values = {
        "market_pk": market_pk,
        "latest_snapshot_id": latest_snapshot_id,
        "latest_snapshot_ts": latest_snapshot_ts or datetime.now(timezone.utc),
        "last_price_cents": _cents(last_price_dollars),
        "yes_bid_cents": _cents(yes_bid_dollars),
        "yes_ask_cents": _cents(yes_ask_dollars),
        "no_bid_cents": _cents(no_bid_dollars),
        "no_ask_cents": _cents(no_ask_dollars),
        "volume_24h_contracts": _contracts(volume_24h_fp),
        "open_interest_contracts": _contracts(open_interest_fp),
        "liquidity_cents": _cents(liquidity_dollars),
        "urgency_score": urgency,
        "evidence_score": evidence,
        "storage_tier": decision.tier,
        "retention_score": decision.score,
        "retention_reasons": list(decision.reasons),
        "updated_at": func.now(),
    }
    stmt = pg_insert(MarketMetric).values(**values)
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
            "storage_tier": excluded.storage_tier,
            "retention_score": excluded.retention_score,
            "retention_reasons": excluded.retention_reasons,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)
    _maybe_heartbeat_retention_projection(decision)


def bump_trade_metrics(db: Session, *, market_pk: int, trade_ts: datetime) -> None:
    stmt = pg_insert(MarketMetric).values(
        market_pk=market_pk,
        trade_count=1,
        last_trade_ts=trade_ts,
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["market_pk"],
        set_={
            "trade_count": MarketMetric.trade_count + 1,
            "last_trade_ts": func.greatest(
                func.coalesce(MarketMetric.last_trade_ts, trade_ts),
                trade_ts,
            ),
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)


def bump_anomaly_metrics(
    db: Session,
    *,
    market_pk: int,
    anomaly_ts: datetime,
    prior: str | None,
    severity: str | None,
) -> None:
    high_increment = 1 if str(severity or "").lower() in {"high", "critical"} else 0
    pr = prior_rank(prior)
    stmt = pg_insert(MarketMetric).values(
        market_pk=market_pk,
        anomaly_count=1,
        high_anomaly_count=high_increment,
        last_anomaly_ts=anomaly_ts,
        evidence_score=evidence_score_0_100(anomaly_count=1),
        urgency_score=urgency_score_0_100(prior_rank=pr, anomaly_count=1),
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["market_pk"],
        set_={
            "anomaly_count": MarketMetric.anomaly_count + 1,
            "high_anomaly_count": MarketMetric.high_anomaly_count + high_increment,
            "last_anomaly_ts": func.greatest(
                func.coalesce(MarketMetric.last_anomaly_ts, anomaly_ts),
                anomaly_ts,
            ),
            "evidence_score": func.greatest(
                MarketMetric.evidence_score,
                stmt.excluded.evidence_score,
            ),
            "urgency_score": func.greatest(
                MarketMetric.urgency_score,
                stmt.excluded.urgency_score,
            ),
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)
