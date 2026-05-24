"""Persist contextual trade flags and promote evidence windows."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import (
    CaseEvidence,
    Market,
    MarketMetric,
    NewsArticle,
    NewsEvent,
    Trade,
    TradeFlag,
)
from app.services.classifier import CLASSIFIER_VERSION
from app.services.market_taxonomy import normalized_category_for_market
from app.services.surveillance_scores import (
    evidence_score_0_100,
    prior_rank,
    urgency_score_0_100,
)
from app.services.trade_baselines import latest_baseline_for_market
from app.services.trade_context import MarketContext, explain_trades_with_context
from app.services.trade_suspicion import explain_trades_against_window
from app.services.quote_series import (
    history_points_for_market,
    quote_payload,
    sibling_history_payloads,
)

TRADE_SCORER_VERSION = 4
TRADE_FLAG_MIN_SCORE = 3.0
TRADE_FLAG_TRIGGERED_SCORE = 7.0
TRADE_FLAG_CASE_SCORE = 8.5


def severity_for_score(score: float) -> str:
    if score >= TRADE_FLAG_CASE_SCORE:
        return "critical"
    if score >= TRADE_FLAG_TRIGGERED_SCORE:
        return "high"
    if score >= 5.0:
        return "medium"
    return "low"


def final_trade_flag_score(
    local_score: float,
    context_score: float,
    context_exp: dict | None,
) -> float:
    score = max(local_score, context_score)
    features = (
        context_exp.get("features", {})
        if isinstance(context_exp, dict)
        else {}
    )
    discount = features.get("post_news_discount_multiplier")
    try:
        discount_value = float(discount)
    except (TypeError, ValueError):
        discount_value = None
    if discount_value is not None and 0 < discount_value < 1:
        score = max(context_score, local_score * discount_value)
    return round(min(10.0, max(0.0, score)), 3)


def _market_context(market: Market) -> MarketContext:
    return MarketContext(
        market_pk=market.id,
        market_id=market.market_id,
        category=normalized_category_for_market(
            category=market.category,
            event_id=market.event_id,
            market_id=market.market_id,
            title=market.title,
        ),
        subcategory=market.subcategory,
        manipulability_prior=market.manipulability_prior,
        event_id=market.event_id,
        close_time=market.close_time,
    )


def _trade_payload(trade: Trade) -> dict:
    trade_price = (
        trade.no_price_dollars
        if (trade.taker_side or "").lower() == "no"
        else trade.yes_price_dollars
    )
    trade_dollars = (
        float(trade.count_fp) * float(trade_price)
        if trade.count_fp is not None and trade_price is not None
        else None
    )
    return {
        "ts": trade.ts.isoformat() if trade.ts else None,
        "yes_price": float(trade.yes_price_dollars)
        if trade.yes_price_dollars is not None
        else None,
        "no_price": float(trade.no_price_dollars)
        if trade.no_price_dollars is not None
        else None,
        "count": float(trade.count_fp) if trade.count_fp is not None else None,
        "trade_dollar_amount": trade_dollars,
        "taker_side": trade.taker_side,
    }


def _news_context_for_market(db: Session, market: Market) -> list[dict]:
    rows = (
        db.query(NewsEvent, NewsArticle)
        .join(NewsArticle, NewsArticle.id == NewsEvent.article_id)
        .filter(NewsEvent.market_pk == market.id)
        .filter(NewsEvent.relevance_score >= 0.35)
        .order_by(NewsArticle.first_seen_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "article_id": article.id,
            "first_seen_at": article.first_seen_at.isoformat()
            if article.first_seen_at
            else None,
            "published_at": article.published_at.isoformat()
            if article.published_at
            else None,
            "title": article.title,
            "domain": article.domain,
            "relevance_score": float(event.relevance_score or 0.0),
            "pre_news_trade_score": float(event.pre_news_trade_score or 0.0),
        }
        for event, article in rows
    ]


def _sibling_snapshot_context(
    db: Session,
    market: Market,
    *,
    start: datetime,
    end: datetime,
) -> list[dict]:
    return sibling_history_payloads(
        db,
        market,
        start=start - timedelta(minutes=15),
        end=end + timedelta(minutes=45),
        sibling_limit=25,
        row_limit=3000,
    )


def _snapshots_for_market(
    db: Session,
    market_pk: int,
    *,
    start: datetime,
    end: datetime,
) -> list[dict]:
    rows = history_points_for_market(
        db,
        market_pk,
        start=start - timedelta(minutes=15),
        end=end + timedelta(minutes=45),
        limit=3000,
    )
    return [quote_payload(row) for row in rows]


def _upsert_flag(
    db: Session,
    *,
    trade: Trade,
    market: Market,
    score: float,
    local_score: float,
    context_score: float,
    reasons: list[str],
    components: dict,
    features: dict,
    scorer_version: int,
) -> int:
    stmt = pg_insert(TradeFlag).values(
        trade_pk=trade.id,
        trade_id=trade.trade_id,
        market_pk=market.id,
        ts=trade.ts,
        score=score,
        local_score=local_score,
        context_score=context_score,
        severity=severity_for_score(score),
        reasons=reasons,
        components=components,
        features=features,
        scorer_version=scorer_version,
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_trade_flags_trade_version",
        set_={
            "score": stmt.excluded.score,
            "local_score": stmt.excluded.local_score,
            "context_score": stmt.excluded.context_score,
            "severity": stmt.excluded.severity,
            "reasons": stmt.excluded.reasons,
            "components": stmt.excluded.components,
            "features": stmt.excluded.features,
            "updated_at": func.now(),
        },
    ).returning(TradeFlag.id)
    return int(db.execute(stmt).scalar_one())


def _upsert_metric_promotion(
    db: Session,
    *,
    market: Market,
    tier: str,
    score: float,
    reasons: list[str],
) -> None:
    pr = prior_rank(market.manipulability_prior)
    stmt = pg_insert(MarketMetric).values(
        market_pk=market.id,
        storage_tier=tier,
        retention_score=int(score * 10),
        retention_reasons=reasons,
        evidence_score=evidence_score_0_100(anomaly_count=1),
        urgency_score=urgency_score_0_100(prior_rank=pr, anomaly_count=1),
        updated_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["market_pk"],
        set_={
            "storage_tier": stmt.excluded.storage_tier,
            "retention_score": func.greatest(
                MarketMetric.retention_score, stmt.excluded.retention_score
            ),
            "retention_reasons": stmt.excluded.retention_reasons,
            "updated_at": func.now(),
        },
    )
    db.execute(stmt)


def _promote_flag_if_needed(
    db: Session,
    *,
    flag_id: int,
    trade: Trade,
    market: Market,
    score: float,
    reasons: list[str],
    components: dict,
    features: dict,
    scorer_version: int,
) -> tuple[str | None, int | None]:
    if score < TRADE_FLAG_TRIGGERED_SCORE:
        return None, None

    tier = (
        "case"
        if score >= TRADE_FLAG_CASE_SCORE or "pre_news_directional_move" in reasons
        else "triggered"
    )
    case_id: int | None = None
    if tier == "case":
        case_key = f"trade_flag:{scorer_version}:{trade.trade_id}"
        stmt = pg_insert(CaseEvidence).values(
            case_key=case_key,
            market_pk=market.id,
            trigger_kind="trade_flag",
            trigger_ts=trade.ts,
            window_start=trade.ts - timedelta(hours=24),
            window_end=trade.ts + timedelta(hours=6),
            payload={
                "trade_id": trade.trade_id,
                "score": score,
                "reasons": reasons,
                "components": components,
                "features": features,
            },
            classifier_version=CLASSIFIER_VERSION,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["case_key"],
            set_={
                "payload": stmt.excluded.payload,
                "window_start": stmt.excluded.window_start,
                "window_end": stmt.excluded.window_end,
            },
        ).returning(CaseEvidence.id)
        case_id = int(db.execute(stmt).scalar_one())

    row = db.query(TradeFlag).filter(TradeFlag.id == flag_id).one()
    row.promoted_storage_tier = tier
    row.case_evidence_id = case_id
    _upsert_metric_promotion(
        db,
        market=market,
        tier=tier,
        score=score,
        reasons=reasons,
    )
    return tier, case_id


def _flags_by_reason(rows: Iterable[TradeFlag]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        for reason in row.reasons or []:
            out[str(reason)] = out.get(str(reason), 0) + 1
    return out


def materialize_trade_flags(
    db: Session,
    *,
    max_trades: int = 5000,
    min_score: float = TRADE_FLAG_MIN_SCORE,
    scorer_version: int = TRADE_SCORER_VERSION,
    market_id: str | None = None,
    dry_run: bool = False,
) -> dict:
    q = db.query(Trade, Market).join(Market, Market.id == Trade.market_pk)
    if market_id:
        q = q.filter(Market.market_id == market_id)
    rows = q.order_by(Trade.ts.desc(), Trade.id.desc()).limit(max_trades).all()
    rows = list(reversed(rows))

    grouped: dict[int, list[tuple[Trade, Market]]] = defaultdict(list)
    for trade, market in rows:
        grouped[market.id].append((trade, market))

    created_or_updated = 0
    promoted = 0
    top: list[dict] = []

    for market_rows in grouped.values():
        market = market_rows[0][1]
        trades = [trade for trade, _market in market_rows]
        payloads = [_trade_payload(trade) for trade in trades]
        local = explain_trades_against_window(payloads, window=50)
        start = trades[0].ts
        end = trades[-1].ts
        context = explain_trades_with_context(
            payloads,
            market=_market_context(market),
            snapshots=_snapshots_for_market(db, market.id, start=start, end=end),
            local_explanations=local,
            peer_baseline=latest_baseline_for_market(
                db, market, scorer_version=scorer_version
            ),
            news_events=_news_context_for_market(db, market),
            sibling_snapshots=_sibling_snapshot_context(
                db, market, start=start, end=end
            ),
        )
        for trade, local_exp, context_exp in zip(trades, local, context):
            local_score = float(local_exp["score"]) if local_exp else 0.0
            context_score = float(context_exp["score"]) if context_exp else 0.0
            score = final_trade_flag_score(local_score, context_score, context_exp)
            if score < min_score:
                continue

            reasons = sorted(
                set(
                    (local_exp or {}).get("reasons", [])
                    + (context_exp or {}).get("reasons", [])
                )
            )
            components = (context_exp or {}).get("components", {})
            features = {
                **((local_exp or {}).get("features", {})),
                "context": (context_exp or {}).get("features", {}),
            }
            top.append(
                {
                    "trade_id": trade.trade_id,
                    "market_id": market.market_id,
                    "score": score,
                    "reasons": reasons,
                }
            )
            if dry_run:
                continue
            flag_id = _upsert_flag(
                db,
                trade=trade,
                market=market,
                score=score,
                local_score=local_score,
                context_score=context_score,
                reasons=reasons,
                components=components,
                features=features,
                scorer_version=scorer_version,
            )
            created_or_updated += 1
            tier, _case_id = _promote_flag_if_needed(
                db,
                flag_id=flag_id,
                trade=trade,
                market=market,
                score=score,
                reasons=reasons,
                components=components,
                features=features,
                scorer_version=scorer_version,
            )
            if tier:
                promoted += 1

    top.sort(key=lambda x: x["score"], reverse=True)
    if not dry_run:
        db.commit()
    recent_flags = (
        db.query(TradeFlag)
        .filter(TradeFlag.scorer_version == scorer_version)
        .order_by(TradeFlag.score.desc())
        .limit(500)
        .all()
        if not dry_run
        else []
    )
    return {
        "scorer_version": scorer_version,
        "sampled_trades": len(rows),
        "flagged": len(top),
        "created_or_updated": created_or_updated,
        "promoted": promoted,
        "top": top[:100],
        "reason_counts": _flags_by_reason(recent_flags),
    }
