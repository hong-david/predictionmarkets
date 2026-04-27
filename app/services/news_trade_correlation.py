"""Batch materialization of news/trade timing correlations.

This service turns a linked ``NewsEvent`` into an explainable pre-news trade
score. It is intentionally model-free: direction, timing, local trade
unusualness, and existing materialized trade flags are combined with simple
rules so the score is auditable.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

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
from app.db.session import SessionLocal
from app.services.classifier import CLASSIFIER_VERSION
from app.services.surveillance_scores import (
    evidence_score_0_100,
    prior_rank,
    urgency_score_0_100,
)
from app.services.trade_suspicion import explain_trades_against_window

NEWS_TRADE_SCORER_VERSION = 1
NEWS_TRADE_MIN_RELEVANCE = 0.35
NEWS_TRADE_SIGNAL_SCORE = 4.0
NEWS_TRADE_STRONG_SIGNAL_SCORE = 7.0
NEWS_TRADE_CASE_SCORE = 7.0


@dataclass(frozen=True)
class NewsTradeCorrelationResult:
    score: float
    status: str
    leakage_window_seconds: int | None
    components: dict
    reasons: tuple[str, ...]
    best_trade_id: str | None = None
    best_trade_pk: int | None = None
    best_trade_ts: datetime | None = None

    def as_score_component(self) -> dict:
        return {
            "scorer": f"news_trade_correlation_v{NEWS_TRADE_SCORER_VERSION}",
            "score": self.score,
            "status": self.status,
            "leakage_window_seconds": self.leakage_window_seconds,
            "reasons": list(self.reasons),
            "best_trade_id": self.best_trade_id,
            "best_trade_pk": self.best_trade_pk,
            "best_trade_ts": self.best_trade_ts.isoformat()
            if self.best_trade_ts
            else None,
            **self.components,
        }


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _trade_payload(trade: Trade) -> dict:
    return {
        "ts": trade.ts.isoformat() if trade.ts else None,
        "yes_price": float(trade.yes_price_dollars)
        if trade.yes_price_dollars is not None
        else None,
        "no_price": float(trade.no_price_dollars)
        if trade.no_price_dollars is not None
        else None,
        "count": float(trade.count_fp) if trade.count_fp is not None else None,
        "taker_side": trade.taker_side,
    }


def _market_direction(event: NewsEvent) -> tuple[int, str, float]:
    components = event.score_components or {}
    direction = components.get("market_direction")
    if not isinstance(direction, dict):
        return 0, "ambiguous", 0.0

    label = str(direction.get("label") or "ambiguous")
    confidence = _f(direction.get("confidence")) or 0.0
    if label == "supports_yes":
        return 1, label, confidence
    if label == "supports_no":
        return -1, label, confidence
    return 0, label, confidence


def _side_direction(side: str | None) -> int:
    s = (side or "").lower()
    if s == "yes":
        return 1
    if s == "no":
        return -1
    return 0


def _sign(value: float | None, *, threshold: float = 0.01) -> int:
    if value is None or abs(value) < threshold:
        return 0
    return 1 if value > 0 else -1


def _timing_component(seconds_before_news: float, lookback: timedelta) -> float:
    if seconds_before_news <= 0 or seconds_before_news > lookback.total_seconds():
        return 0.0
    minutes = seconds_before_news / 60.0
    if minutes <= 5:
        return 2.5
    if minutes <= 60:
        return 2.2
    if minutes <= 6 * 60:
        return 1.8
    if minutes <= 24 * 60:
        return 1.1
    return 0.5


def _first_price_at_or_after(
    trades: Sequence[Trade],
    *,
    start_idx: int,
    target_ts: datetime,
) -> float | None:
    fallback: float | None = None
    for row in trades[start_idx + 1 :]:
        ts = _aware(row.ts)
        price = _f(row.yes_price_dollars)
        if ts is None or price is None:
            continue
        if ts >= target_ts:
            return price
        fallback = price
    return fallback


def _trade_flag_map(flags: Sequence[TradeFlag] | Mapping[int, TradeFlag]) -> dict[int, TradeFlag]:
    if isinstance(flags, Mapping):
        return {int(k): v for k, v in flags.items()}
    out: dict[int, TradeFlag] = {}
    for flag in flags:
        current = out.get(flag.trade_pk)
        if current is None or float(flag.score or 0.0) > float(current.score or 0.0):
            out[flag.trade_pk] = flag
    return out


def _status_for_score(score: float) -> str:
    if score >= NEWS_TRADE_STRONG_SIGNAL_SCORE:
        return "strong_pre_news_signal"
    if score >= NEWS_TRADE_SIGNAL_SCORE:
        return "pre_news_signal"
    return "no_pre_news_signal"


def _empty_result(reason: str, *, evaluated_trades: int = 0) -> NewsTradeCorrelationResult:
    return NewsTradeCorrelationResult(
        score=0.0,
        status="no_pre_news_signal",
        leakage_window_seconds=None,
        components={
            "evaluated_trades": evaluated_trades,
            "best_trade": None,
            "component_scores": {},
        },
        reasons=(reason,),
    )


def score_news_trade_correlation(
    *,
    article: NewsArticle,
    event: NewsEvent,
    trades: Sequence[Trade],
    trade_flags: Sequence[TradeFlag] | Mapping[int, TradeFlag] = (),
    lookback: timedelta = timedelta(hours=24),
) -> NewsTradeCorrelationResult:
    """Score whether market activity before an article looks news-aligned."""

    news_ts = _aware(article.first_seen_at) or _aware(article.published_at)
    if news_ts is None:
        return _empty_result("missing_news_timestamp")

    start_ts = news_ts - lookback
    pre_news_trades = [
        trade
        for trade in trades
        if (ts := _aware(trade.ts)) is not None and start_ts <= ts < news_ts
    ]
    if not pre_news_trades:
        return _empty_result("no_pre_news_trades")

    pre_news_trades = sorted(pre_news_trades, key=lambda t: (_aware(t.ts) or news_ts, t.id or 0))
    payloads = [_trade_payload(trade) for trade in pre_news_trades]
    local_explanations = explain_trades_against_window(payloads, window=50)
    flags_by_trade = _trade_flag_map(trade_flags)
    desired_direction, direction_label, direction_confidence = _market_direction(event)

    best: dict | None = None
    previous_price: float | None = None
    for idx, trade in enumerate(pre_news_trades):
        ts = _aware(trade.ts)
        price = _f(trade.yes_price_dollars)
        if ts is None or price is None:
            continue

        seconds_before_news = (news_ts - ts).total_seconds()
        timing = _timing_component(seconds_before_news, lookback)
        if timing <= 0:
            continue

        local_exp = local_explanations[idx] if idx < len(local_explanations) else None
        local_score = float((local_exp or {}).get("score") or 0.0)
        flag = flags_by_trade.get(trade.id)
        flag_score = float(flag.score or 0.0) if flag else 0.0

        prev_price = previous_price if previous_price is not None else price
        price_delta = price - prev_price
        future_price = _first_price_at_or_after(
            pre_news_trades,
            start_idx=idx,
            target_ts=ts + timedelta(minutes=5),
        )
        if future_price is None:
            future_price = price
        pre_news_followthrough = future_price - prev_price
        side_direction = _side_direction(trade.taker_side)
        move_direction = _sign(pre_news_followthrough, threshold=0.02)
        instant_direction = _sign(price_delta, threshold=0.02)

        component_scores = {
            "timing": timing,
            "relevance": round(min(1.0, float(event.relevance_score or 0.0)), 3),
            "direction_alignment": 0.0,
            "trade_signal": round(
                min(3.0, 0.35 * local_score + 0.45 * flag_score), 3
            ),
            "pre_news_move": 0.0,
            "size": 0.0,
        }
        reasons = ["pre_news_trade_timing"]

        count = _f(trade.count_fp)
        if count is not None and count >= 100:
            component_scores["size"] = round(min(0.8, math.log1p(count) / 8.0), 3)
            reasons.append("large_trade_size")

        if local_score >= 3.0:
            reasons.append("local_trade_outlier")
        if flag_score >= 3.0:
            reasons.append("materialized_trade_flag")

        if desired_direction:
            side_aligned = side_direction == desired_direction
            move_aligned = move_direction == desired_direction
            instant_aligned = instant_direction == desired_direction
            side_opposed = side_direction == -desired_direction
            move_opposed = move_direction == -desired_direction

            if side_aligned and (move_aligned or instant_aligned):
                component_scores["direction_alignment"] = round(
                    2.2 * max(0.35, direction_confidence), 3
                )
                reasons.append("direction_aligned_with_news")
            elif side_aligned or move_aligned or instant_aligned:
                component_scores["direction_alignment"] = round(
                    1.4 * max(0.35, direction_confidence), 3
                )
                reasons.append("direction_aligned_with_news")
            elif side_opposed and move_opposed:
                component_scores["direction_alignment"] = -0.8
                reasons.append("opposite_to_news_direction")

            aligned_move = desired_direction * pre_news_followthrough
            if aligned_move >= 0.03:
                component_scores["pre_news_move"] = round(
                    min(1.5, aligned_move * 18.0), 3
                )
                reasons.append("price_moved_toward_news_side")
        elif abs(pre_news_followthrough) >= 0.05:
            component_scores["direction_alignment"] = 0.25
            component_scores["pre_news_move"] = 0.4
            reasons.append("ambiguous_news_with_pre_news_move")

        raw_score = sum(component_scores.values())
        score = round(max(0.0, min(10.0, raw_score)), 3)
        if previous_price is None or price is not None:
            previous_price = price

        candidate = {
            "score": score,
            "seconds_before_news": int(seconds_before_news),
            "trade": trade,
            "component_scores": {k: round(v, 3) for k, v in component_scores.items()},
            "reasons": tuple(sorted(set(reasons))),
            "features": {
                "direction_label": direction_label,
                "direction_confidence": round(direction_confidence, 3),
                "desired_price_direction": desired_direction,
                "side_direction": side_direction,
                "move_direction": move_direction,
                "price_delta": round(price_delta, 4),
                "pre_news_followthrough": round(pre_news_followthrough, 4),
                "local_score": round(local_score, 3),
                "trade_flag_score": round(flag_score, 3),
                "yes_price": round(price, 4),
                "count": round(count, 3) if count is not None else None,
                "taker_side": trade.taker_side,
            },
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate

    if best is None:
        return _empty_result("no_scorable_pre_news_trades", evaluated_trades=len(pre_news_trades))

    trade = best["trade"]
    status = _status_for_score(float(best["score"]))
    return NewsTradeCorrelationResult(
        score=float(best["score"]),
        status=status,
        leakage_window_seconds=int(best["seconds_before_news"]),
        components={
            "evaluated_trades": len(pre_news_trades),
            "lookback_hours": round(lookback.total_seconds() / 3600.0, 3),
            "market_direction_label": direction_label,
            "best_trade": {
                "trade_id": trade.trade_id,
                "trade_pk": trade.id,
                "ts": trade.ts.isoformat() if trade.ts else None,
                **best["features"],
            },
            "component_scores": best["component_scores"],
        },
        reasons=best["reasons"],
        best_trade_id=trade.trade_id,
        best_trade_pk=trade.id,
        best_trade_ts=_aware(trade.ts),
    )


def _trade_flags_for_trades(
    db: Session,
    trades: Sequence[Trade],
    *,
    scorer_version: int | None = None,
) -> list[TradeFlag]:
    trade_ids = [trade.id for trade in trades if trade.id is not None]
    if not trade_ids:
        return []
    q = db.query(TradeFlag).filter(TradeFlag.trade_pk.in_(trade_ids))
    if scorer_version is not None:
        q = q.filter(TradeFlag.scorer_version == scorer_version)
    return q.all()


def _load_trades_for_event(
    db: Session,
    *,
    market_pk: int,
    news_ts: datetime,
    lookback: timedelta,
    max_trades: int,
) -> list[Trade]:
    start_ts = news_ts - lookback
    return (
        db.query(Trade)
        .filter(Trade.market_pk == market_pk)
        .filter(Trade.ts >= start_ts)
        .filter(Trade.ts < news_ts)
        .order_by(Trade.ts.asc(), Trade.id.asc())
        .limit(max_trades)
        .all()
    )


def _load_event_rows(
    db: Session,
    *,
    limit: int,
    after_event_id: int,
    min_relevance: float,
    rescore: bool,
) -> list[tuple[NewsEvent, NewsArticle, Market]]:
    q = (
        db.query(NewsEvent, NewsArticle, Market)
        .join(NewsArticle, NewsArticle.id == NewsEvent.article_id)
        .join(Market, Market.id == NewsEvent.market_pk)
        .filter(NewsEvent.id > after_event_id)
        .filter(NewsEvent.relevance_score >= min_relevance)
    )
    if not rescore:
        q = q.filter(NewsEvent.status == "candidate")
    return q.order_by(NewsEvent.id.asc()).limit(limit).all()


def _merge_components(existing: dict | None, result: NewsTradeCorrelationResult) -> dict:
    components = dict(existing or {})
    components["news_trade_correlation"] = result.as_score_component()
    return components


def _promote_market_metric_for_news_signal(
    db: Session,
    *,
    market: Market,
    score: float,
    tier: str,
    reasons: tuple[str, ...],
) -> None:
    metric = db.query(MarketMetric).filter(MarketMetric.market_pk == market.id).first()
    retention_score = int(round(score * 10))
    retention_reasons = ["news_trade_correlation", *list(reasons)]
    pr = prior_rank(market.manipulability_prior)
    evidence = evidence_score_0_100(anomaly_count=1)
    urgency = urgency_score_0_100(prior_rank=pr, anomaly_count=1)

    if metric is None:
        db.add(
            MarketMetric(
                market_pk=market.id,
                storage_tier=tier,
                retention_score=retention_score,
                retention_reasons=retention_reasons,
                evidence_score=evidence,
                urgency_score=urgency,
            )
        )
        return

    if metric.storage_tier != "case":
        metric.storage_tier = tier
    metric.retention_score = max(int(metric.retention_score or 0), retention_score)
    metric.retention_reasons = retention_reasons
    metric.evidence_score = max(float(metric.evidence_score or 0.0), evidence)
    metric.urgency_score = max(float(metric.urgency_score or 0.0), urgency)


def _promote_news_event_if_needed(
    db: Session,
    *,
    event: NewsEvent,
    article: NewsArticle,
    market: Market,
    result: NewsTradeCorrelationResult,
    lookback: timedelta,
) -> int | None:
    if result.score < NEWS_TRADE_CASE_SCORE:
        return None

    trigger_ts = _aware(article.first_seen_at) or _aware(article.published_at)
    if trigger_ts is None:
        return None

    case_key = f"news_trade:{NEWS_TRADE_SCORER_VERSION}:{event.id}"
    payload = {
        "event_id": event.id,
        "article_id": article.id,
        "article_title": article.title,
        "article_url": article.canonical_url,
        "market_id": market.market_id,
        "market_title": market.title,
        "relevance_score": float(event.relevance_score or 0.0),
        "score_components": result.as_score_component(),
    }
    row = db.query(CaseEvidence).filter(CaseEvidence.case_key == case_key).first()
    if row is None:
        row = CaseEvidence(
            case_key=case_key,
            market_pk=market.id,
            trigger_kind="news_trade_correlation",
            trigger_ts=trigger_ts,
            window_start=trigger_ts - lookback,
            window_end=trigger_ts + timedelta(hours=6),
            payload=payload,
            classifier_version=CLASSIFIER_VERSION,
        )
        db.add(row)
        db.flush()
    else:
        row.trigger_ts = trigger_ts
        row.window_start = trigger_ts - lookback
        row.window_end = trigger_ts + timedelta(hours=6)
        row.payload = payload
        row.classifier_version = CLASSIFIER_VERSION

    _promote_market_metric_for_news_signal(
        db,
        market=market,
        score=result.score,
        tier="case",
        reasons=result.reasons,
    )
    return int(row.id)


def materialize_news_trade_correlation_batch(
    db: Session,
    *,
    limit: int = 100,
    after_event_id: int = 0,
    min_relevance: float = NEWS_TRADE_MIN_RELEVANCE,
    lookback_hours: float = 24.0,
    max_trades_per_event: int = 1000,
    trade_flag_scorer_version: int | None = None,
    rescore: bool = False,
    promote_cases: bool = True,
    dry_run: bool = False,
) -> dict:
    """Process one bounded batch of candidate news events."""

    lookback = timedelta(hours=lookback_hours)
    rows = _load_event_rows(
        db,
        limit=limit,
        after_event_id=after_event_id,
        min_relevance=min_relevance,
        rescore=rescore,
    )
    processed = 0
    updated = 0
    promoted = 0
    status_counts: dict[str, int] = {}
    top: list[dict] = []
    last_event_id = after_event_id

    for event, article, market in rows:
        processed += 1
        last_event_id = max(last_event_id, int(event.id))
        news_ts = _aware(article.first_seen_at) or _aware(article.published_at)
        if news_ts is None:
            trades: list[Trade] = []
            flags: list[TradeFlag] = []
        else:
            trades = _load_trades_for_event(
                db,
                market_pk=event.market_pk,
                news_ts=news_ts,
                lookback=lookback,
                max_trades=max_trades_per_event,
            )
            flags = _trade_flags_for_trades(
                db,
                trades,
                scorer_version=trade_flag_scorer_version,
            )

        result = score_news_trade_correlation(
            article=article,
            event=event,
            trades=trades,
            trade_flags=flags,
            lookback=lookback,
        )
        status_counts[result.status] = status_counts.get(result.status, 0) + 1
        top.append(
            {
                "event_id": event.id,
                "market_id": market.market_id,
                "article_id": article.id,
                "score": result.score,
                "status": result.status,
                "leakage_window_seconds": result.leakage_window_seconds,
                "best_trade_id": result.best_trade_id,
                "reasons": list(result.reasons),
            }
        )

        if dry_run:
            continue

        event.pre_news_trade_score = result.score
        event.leakage_window_seconds = result.leakage_window_seconds
        event.status = result.status
        components = _merge_components(event.score_components, result)
        if promote_cases:
            case_id = _promote_news_event_if_needed(
                db,
                event=event,
                article=article,
                market=market,
                result=result,
                lookback=lookback,
            )
            if case_id is not None:
                promoted += 1
                components["news_trade_correlation"]["promoted_storage_tier"] = "case"
                components["news_trade_correlation"]["case_evidence_id"] = case_id
        event.score_components = components
        updated += 1

    if not dry_run:
        db.commit()

    top.sort(key=lambda row: float(row["score"]), reverse=True)
    return {
        "scorer_version": NEWS_TRADE_SCORER_VERSION,
        "processed": processed,
        "updated": updated,
        "promoted": promoted,
        "status_counts": status_counts,
        "top": top[:50],
        "last_event_id": last_event_id,
    }


def _merge_batch_results(results: list[dict]) -> dict:
    processed = sum(int(result.get("processed", 0)) for result in results)
    updated = sum(int(result.get("updated", 0)) for result in results)
    promoted = sum(int(result.get("promoted", 0)) for result in results)
    status_counts: dict[str, int] = {}
    top: list[dict] = []
    last_event_id = 0
    for result in results:
        last_event_id = max(last_event_id, int(result.get("last_event_id", 0)))
        for status, count in (result.get("status_counts") or {}).items():
            status_counts[str(status)] = status_counts.get(str(status), 0) + int(count)
        top.extend(result.get("top") or [])
    top.sort(key=lambda row: float(row["score"]), reverse=True)
    return {
        "scorer_version": NEWS_TRADE_SCORER_VERSION,
        "processed": processed,
        "updated": updated,
        "promoted": promoted,
        "batches": len(results),
        "status_counts": status_counts,
        "top": top[:100],
        "last_event_id": last_event_id,
    }


def materialize_news_trade_correlations(
    db: Session,
    *,
    max_events: int = 1000,
    batch_size: int = 100,
    min_relevance: float = NEWS_TRADE_MIN_RELEVANCE,
    lookback_hours: float = 24.0,
    max_trades_per_event: int = 1000,
    trade_flag_scorer_version: int | None = None,
    rescore: bool = False,
    promote_cases: bool = True,
    dry_run: bool = False,
) -> dict:
    """Process candidate news events in bounded batches using one session."""

    results: list[dict] = []
    after_event_id = 0
    remaining = max(0, max_events)
    while remaining > 0:
        result = materialize_news_trade_correlation_batch(
            db,
            limit=min(batch_size, remaining),
            after_event_id=after_event_id,
            min_relevance=min_relevance,
            lookback_hours=lookback_hours,
            max_trades_per_event=max_trades_per_event,
            trade_flag_scorer_version=trade_flag_scorer_version,
            rescore=rescore,
            promote_cases=promote_cases,
            dry_run=dry_run,
        )
        results.append(result)
        processed = int(result["processed"])
        if processed == 0:
            break
        after_event_id = int(result["last_event_id"])
        remaining -= processed
    return _merge_batch_results(results)


def _run_batch_with_new_session(
    session_factory: Callable[[], Session],
    kwargs: dict,
) -> dict:
    db = session_factory()
    try:
        return materialize_news_trade_correlation_batch(db, **kwargs)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def materialize_news_trade_correlations_async(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    max_events: int = 1000,
    batch_size: int = 100,
    min_relevance: float = NEWS_TRADE_MIN_RELEVANCE,
    lookback_hours: float = 24.0,
    max_trades_per_event: int = 1000,
    trade_flag_scorer_version: int | None = None,
    rescore: bool = False,
    promote_cases: bool = True,
    dry_run: bool = False,
    sleep_seconds: float = 0.0,
) -> dict:
    """Async batch runner around the sync SQLAlchemy materializer.

    Each batch gets its own session and commits independently. That keeps the
    runner friendly to a long-lived worker process without requiring an async
    SQLAlchemy migration.
    """

    results: list[dict] = []
    after_event_id = 0
    remaining = max(0, max_events)
    while remaining > 0:
        kwargs = {
            "limit": min(batch_size, remaining),
            "after_event_id": after_event_id,
            "min_relevance": min_relevance,
            "lookback_hours": lookback_hours,
            "max_trades_per_event": max_trades_per_event,
            "trade_flag_scorer_version": trade_flag_scorer_version,
            "rescore": rescore,
            "promote_cases": promote_cases,
            "dry_run": dry_run,
        }
        result = await asyncio.to_thread(
            _run_batch_with_new_session,
            session_factory,
            kwargs,
        )
        results.append(result)
        processed = int(result["processed"])
        if processed == 0:
            break
        after_event_id = int(result["last_event_id"])
        remaining -= processed
        if sleep_seconds > 0 and remaining > 0:
            await asyncio.sleep(sleep_seconds)
    return _merge_batch_results(results)


def decimal_price(value: float) -> Decimal:
    """Small helper for tests and scripts that build synthetic trade rows."""

    return Decimal(str(value))
