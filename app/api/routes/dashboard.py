"""Dashboard JSON API.

This module is the read-side surface for the React frontend in `frontend/`.
It is intentionally JSON-only — no embedded HTML, no templating. The
frontend is built by Vite and either served by Vite's dev server (which
proxies `/api/*` here) or, in production, mounted as static files from
`frontend/dist/` by `app/main.py`.

Endpoint conventions:
  - All paths are under `/api/dashboard/...` so they're trivially
    proxiable and don't collide with public-API routes (`/markets`,
    `/anomalies`).
  - Pagination uses `limit` + `offset` (not cursor) because the
    frontend's TanStack Table needs a total count for "page N of M"
    style UI.
  - Every response is a single JSON object (never a bare list) so we
    can add metadata (totals, provider info, error fields) without
    breaking clients.

Performance notes:
  - The DB is small enough at v1 scale (~24k markets, ~135k trades,
    ~800k book events) that fresh `count(*)` and `GROUP BY` queries on
    every request take low single-digit milliseconds. If this changes
    we'd add a 30-second materialised-view or in-process LRU here, not
    a redis layer.
  - The series endpoint caps trade rows at 2000 by default. A 30-second
    BTC strike has ~10 trades; an active 4-hour NBA game has ~2k. The
    cap keeps the wire payload small enough for `lightweight-charts` to
    render without virtualisation.
"""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Callable, TypeVar

import httpx
import orjson
import redis
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, desc, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import settings
from app.db.models import (
    Anomaly,
    Market,
    MarketMetric,
    MarketNewsProfile,
    MarketSnapshot,
    NewsArticle,
    NewsEvent,
    PipelineHeartbeat,
    Trade,
    TradeFlag,
)
from app.services.historical_signal_qa import historical_signal_report
from app.services.news_correlation import market_news_search_query, profile_for_market
from app.services.news_direction import components_with_market_direction
from app.services.news_gdelt import build_gdelt_query, choose_news_window
from app.services.news_relevance import hybrid_news_relevance
from app.services.news_source_registry import source_registry_diagnostics
from app.services.market_lifecycle import (
    market_lifecycle as _market_lifecycle,
    market_scope_filters as _market_scope_filters,
    normalize_market_scope as _normalize_market_scope,
)
from app.services.search_index import dashboard_search
from app.services.storage_health import storage_health_detail, storage_health_snapshot
from app.services.surveillance_scores import (
    aggregate_reason_codes,
    collect_reason_strings_from_anomaly_json,
    evidence_score_0_100,
    market_priority_value,
    prior_rank,
    urgency_score_0_100,
)
from app.services.trade_burst import analyze_tape_bursts
from app.services.trade_context import (
    MarketContext,
    build_peer_baselines,
    explain_trades_with_context,
)
from app.services.trade_suspicion import explain_trades_against_window

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
logger = logging.getLogger(__name__)

T = TypeVar("T")
_DASHBOARD_CACHE_TTL_SEC = 30.0
_TOP_MARKETS_RECENT_TRADE_SAMPLE = 50_000
_SUSPICIOUS_TRADE_SAMPLE = 20_000
_PIPELINE_WS_STALE_AFTER = timedelta(hours=4)
_PIPELINE_MARKET_POLLER_STALE_AFTER = timedelta(hours=6)
_PIPELINE_DAILY_STALE_AFTER = timedelta(hours=24)
_dashboard_cache_lock = Lock()
_dashboard_cache: dict[str, tuple[float, object]] = {}
_redis_client: redis.Redis | None = None

def _cached_dashboard_payload(key: str, build: Callable[[], T]) -> T:
    now = time.monotonic()
    r = _dashboard_redis()
    redis_key = f"dashboard:{key}"
    if r is not None:
        try:
            raw = r.get(redis_key)
            if raw:
                return orjson.loads(raw)  # type: ignore[return-value]
        except Exception as exc:
            logger.debug("dashboard redis cache read failed: %s", exc)

    with _dashboard_cache_lock:
        cached = _dashboard_cache.get(key)
        if cached and now - cached[0] < _DASHBOARD_CACHE_TTL_SEC:
            return cached[1]  # type: ignore[return-value]

    payload = build()
    if r is not None:
        try:
            r.setex(redis_key, int(_DASHBOARD_CACHE_TTL_SEC), orjson.dumps(payload))
        except Exception as exc:
            logger.debug("dashboard redis cache write failed: %s", exc)
    with _dashboard_cache_lock:
        _dashboard_cache[key] = (time.monotonic(), payload)
    return payload


def _dashboard_redis() -> redis.Redis | None:
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        _redis_client = redis.Redis.from_url(settings.redis_url, socket_timeout=0.15)
        return _redis_client
    except Exception as exc:
        logger.debug("dashboard redis unavailable: %s", exc)
        return None


# --- helpers ----------------------------------------------------------------

# `manipulability_prior` is a bucketed string column; we want a numeric
# rank for ORDER BY when the user sorts "by priority". Higher = more
# attention-worthy. Anything outside this map (NULL, weird value) sorts
# last via the COALESCE in `_prior_rank()`.
_PRIOR_RANK = {
    "very_low": 0,
    "low": 1,
    "medium": 2,
    "medium_high": 3,
    "high": 4,
}


def _prior_rank() -> case:
    """SQLAlchemy CASE expression that maps prior strings to integer rank."""
    return case(_PRIOR_RANK, value=Market.manipulability_prior, else_=-1)


def _trade_notional_dollars(
    *,
    yes_price: object,
    no_price: object,
    count: object,
    taker_side: str | None,
) -> float | None:
    """Estimated dollars paid for the contracts in one public trade print."""
    if count is None:
        return None
    side = (taker_side or "").lower()
    price = no_price if side == "no" and no_price is not None else yes_price
    if price is None:
        price = no_price
    if price is None:
        return None
    try:
        return float(count) * float(price)
    except (TypeError, ValueError):
        return None


def _probability_float(value: object) -> float | None:
    """Finite probability-like value clamped to Kalshi's [0, 1] range."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return min(1.0, max(0.0, out))


def _reason_codes_for_market_pks(
    db: Session, market_pks: list[int]
) -> dict[int, list[str]]:
    """Snake_case reason codes (deduped) from all `anomalies.reasons` JSON for a market."""
    if not market_pks:
        return {}
    rows = (
        db.query(Anomaly.market_pk, Anomaly.reasons)
        .filter(Anomaly.market_pk.in_(market_pks))
        .all()
    )
    by_m: dict[int, list[str]] = defaultdict(list)
    for m_pk, reasons in rows:
        for r in collect_reason_strings_from_anomaly_json(reasons):
            by_m[m_pk].append(r)
    return {k: aggregate_reason_codes(v)[:24] for k, v in by_m.items()}


def _serialize_market_row(
    market: Market,
    *,
    trade_count: int = 0,
    anomaly_count: int = 0,
    last_price: float | None = None,
    volume_24h: float | None = None,
    trade_dollar_volume: float | None = None,
    reason_codes: list[str] | None = None,
    event_market_count: int | None = None,
    evidence_score: float | None = None,
    urgency_score: float | None = None,
    top_trade_flag_score: float | None = None,
    storage_tier: str | None = None,
    retention_score: int | None = None,
) -> dict:
    prk = prior_rank(market.manipulability_prior)
    ac = int(anomaly_count or 0)
    lifecycle = _market_lifecycle(market)
    return {
        "market_id": market.market_id,
        "event_id": market.event_id,
        "title": market.title,
        "subtitle": market.subtitle,
        "status": market.status,
        "category": market.category,
        "subcategory": market.subcategory,
        "manipulability_prior": market.manipulability_prior,
        "classifier_confidence": market.classifier_confidence,
        "classifier_layer": market.classifier_layer,
        "classifier_rule": market.classifier_rule,
        "open_time": market.open_time.isoformat() if market.open_time else None,
        "close_time": market.close_time.isoformat() if market.close_time else None,
        "market_lifecycle": lifecycle,
        "is_active": lifecycle == "active",
        "trade_count": int(trade_count or 0),
        "anomaly_count": ac,
        "last_price": _probability_float(last_price),
        "volume_24h": float(volume_24h) if volume_24h is not None else None,
        "trade_dollar_volume": float(trade_dollar_volume)
        if trade_dollar_volume is not None
        else None,
        "market_priority": market_priority_value(market.manipulability_prior),
        "evidence_score": float(evidence_score)
        if evidence_score is not None
        else evidence_score_0_100(anomaly_count=ac),
        "urgency_score": float(urgency_score)
        if urgency_score is not None
        else urgency_score_0_100(prior_rank=prk, anomaly_count=ac),
        "top_trade_flag_score": float(top_trade_flag_score)
        if top_trade_flag_score is not None
        else None,
        "reasons": list(reason_codes or []),
        "event_market_count": event_market_count,
        "storage_tier": storage_tier,
        "retention_score": retention_score,
    }


def _market_context(market: Market) -> MarketContext:
    return MarketContext(
        market_pk=market.id,
        market_id=market.market_id,
        category=market.category,
        subcategory=market.subcategory,
        manipulability_prior=market.manipulability_prior,
        event_id=market.event_id,
        close_time=market.close_time,
    )


def _latest_snapshot_values_for_market_pks(
    db: Session, market_pks: list[int]
) -> dict[int, dict[str, float | None]]:
    if not market_pks:
        return {}
    ranked = (
        select(
            MarketSnapshot.market_pk.label("market_pk"),
            MarketSnapshot.last_price_dollars.label("last_price"),
            MarketSnapshot.yes_bid_dollars.label("yes_bid"),
            MarketSnapshot.yes_ask_dollars.label("yes_ask"),
            MarketSnapshot.volume_24h_fp.label("volume_24h"),
            func.row_number()
            .over(
                partition_by=MarketSnapshot.market_pk,
                order_by=(MarketSnapshot.ts.desc(), MarketSnapshot.id.desc()),
            )
            .label("rn"),
        )
        .where(MarketSnapshot.market_pk.in_(market_pks))
        .subquery()
    )
    rows = db.execute(
        select(
            ranked.c.market_pk,
            ranked.c.last_price,
            ranked.c.yes_bid,
            ranked.c.yes_ask,
            ranked.c.volume_24h,
        ).where(ranked.c.rn == 1)
    ).all()

    def _display_price(row) -> float | None:
        if row.last_price is not None:
            return _probability_float(row.last_price)
        if row.yes_bid is not None and row.yes_ask is not None:
            return _probability_float((float(row.yes_bid) + float(row.yes_ask)) / 2.0)
        if row.yes_bid is not None:
            return _probability_float(row.yes_bid)
        if row.yes_ask is not None:
            return _probability_float(row.yes_ask)
        return None

    values = {
        int(row.market_pk): {
            "last_price": _display_price(row),
            "volume_24h": float(row.volume_24h)
            if row.volume_24h is not None
            else None,
        }
        for row in rows
    }
    missing_or_blank = [
        pk
        for pk in market_pks
        if pk not in values
        or (
            values[pk].get("last_price") is None
            and values[pk].get("volume_24h") is None
        )
    ]
    if missing_or_blank:
        metric_rows = (
            db.query(
                MarketMetric.market_pk,
                MarketMetric.last_price_cents,
                MarketMetric.volume_24h_contracts,
            )
            .filter(MarketMetric.market_pk.in_(missing_or_blank))
            .all()
        )
        for row in metric_rows:
            current = values.setdefault(
                int(row.market_pk), {"last_price": None, "volume_24h": None}
            )
            if current["last_price"] is None and row.last_price_cents is not None:
                current["last_price"] = _probability_float(
                    float(row.last_price_cents) / 100.0
                )
            if current["volume_24h"] is None and row.volume_24h_contracts is not None:
                current["volume_24h"] = float(row.volume_24h_contracts)
    price_missing = [
        pk for pk in market_pks if values.get(pk, {}).get("last_price") is None
    ]
    if price_missing:
        ranked_trades = (
            select(
                Trade.market_pk.label("market_pk"),
                Trade.yes_price_dollars.label("yes_price"),
                func.row_number()
                .over(
                    partition_by=Trade.market_pk,
                    order_by=(Trade.ts.desc(), Trade.id.desc()),
                )
                .label("rn"),
            )
            .where(Trade.market_pk.in_(price_missing))
            .subquery()
        )
        trade_rows = db.execute(
            select(ranked_trades.c.market_pk, ranked_trades.c.yes_price).where(
                ranked_trades.c.rn == 1
            )
        ).all()
        for row in trade_rows:
            if row.yes_price is None:
                continue
            current = values.setdefault(
                int(row.market_pk), {"last_price": None, "volume_24h": None}
            )
            if current["last_price"] is None:
                current["last_price"] = _probability_float(row.yes_price)
    return values


def _snapshot_payload(snapshot: MarketSnapshot) -> dict:
    return {
        "ts": snapshot.ts.isoformat() if snapshot.ts else None,
        "market_pk": snapshot.market_pk,
        "yes_bid": _probability_float(snapshot.yes_bid_dollars),
        "yes_ask": _probability_float(snapshot.yes_ask_dollars),
        "last_price": _probability_float(snapshot.last_price_dollars),
        "volume_24h": float(snapshot.volume_24h_fp)
        if snapshot.volume_24h_fp is not None
        else None,
        "open_interest": float(snapshot.open_interest_fp)
        if snapshot.open_interest_fp is not None
        else None,
    }


def _peer_baseline_rows_for_market(
    db: Session,
    market: Market,
    *,
    limit: int = 5000,
) -> list[dict]:
    if not market.category:
        return []
    q = (
        db.query(Trade, Market)
        .join(Market, Market.id == Trade.market_pk)
        .filter(Market.category == market.category)
    )
    if market.subcategory:
        q = q.filter(Market.subcategory == market.subcategory)
    rows = q.order_by(Trade.ts.desc(), Trade.id.desc()).limit(limit).all()
    out = [
        {
            "market_pk": t.market_pk,
            "category": m.category,
            "subcategory": m.subcategory,
            "ts": t.ts.isoformat() if t.ts else None,
            "yes_price": _probability_float(t.yes_price_dollars),
            "count": float(t.count_fp) if t.count_fp is not None else None,
        }
        for t, m in reversed(rows)
    ]
    return out


def _news_context_for_market(db: Session, market: Market) -> list[dict]:
    rows = (
        db.query(NewsEvent, NewsArticle)
        .join(NewsArticle, NewsArticle.id == NewsEvent.article_id)
        .filter(NewsEvent.market_pk == market.id)
        .filter(NewsEvent.relevance_score >= 0.35)
        .order_by(NewsArticle.first_seen_at.desc())
        .limit(25)
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
    start: datetime | None,
    end: datetime | None,
    limit: int = 3000,
) -> list[dict]:
    if not market.event_id or start is None or end is None:
        return []
    sibling_pks = [
        int(pk)
        for (pk,) in (
            db.query(Market.id)
            .filter(Market.event_id == market.event_id)
            .filter(Market.id != market.id)
            .limit(25)
            .all()
        )
    ]
    if not sibling_pks:
        return []
    rows = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.market_pk.in_(sibling_pks))
        .filter(MarketSnapshot.ts >= start - timedelta(minutes=15))
        .filter(MarketSnapshot.ts <= end + timedelta(minutes=45))
        .order_by(MarketSnapshot.ts.asc(), MarketSnapshot.id.asc())
        .limit(limit)
        .all()
    )
    return [_snapshot_payload(row) for row in rows]


# --- /pipeline-health -------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _latest_datetime(*values: datetime | None) -> datetime | None:
    known = [_as_utc(v) for v in values if v is not None]
    if not known:
        return None
    return max(known)


def _age_seconds(latest_at: datetime | None, *, now: datetime) -> int | None:
    latest = _as_utc(latest_at)
    if latest is None:
        return None
    return max(0, int((now - latest).total_seconds()))


def _pipeline_component(
    *,
    key: str,
    label: str,
    status: str,
    latest_at: datetime | None,
    age_seconds: int | None,
    count: int | None,
    description: str,
    detail: str,
    heartbeat_at: datetime | None = None,
    last_success_at: datetime | None = None,
    last_error_at: datetime | None = None,
    last_error: str | None = None,
    component_type: str | None = None,
    source: str = "db",
    run_id: str | None = None,
) -> dict:
    latest = _as_utc(latest_at)
    return {
        "key": key,
        "label": label,
        "status": status,
        "latest_at": latest.isoformat() if latest else None,
        "age_seconds": age_seconds,
        "count": count,
        "description": description,
        "detail": detail,
        "heartbeat_at": _as_utc(heartbeat_at).isoformat() if heartbeat_at else None,
        "last_success_at": _as_utc(last_success_at).isoformat()
        if last_success_at
        else None,
        "last_error_at": _as_utc(last_error_at).isoformat()
        if last_error_at
        else None,
        "last_error": last_error,
        "component_type": component_type,
        "source": source,
        "run_id": run_id,
    }


def _freshness_status(
    *,
    latest_at: datetime | None,
    count: int | None,
    stale_after: timedelta,
    now: datetime,
) -> str:
    if count is not None and count <= 0:
        return "empty"
    if latest_at is None:
        return "empty"
    age = _age_seconds(latest_at, now=now)
    if age is not None and age > int(stale_after.total_seconds()):
        return "stale"
    return "healthy"


def _safe_pipeline_component(
    db: Session,
    *,
    key: str,
    label: str,
    description: str,
    build: Callable[[], dict],
) -> dict:
    try:
        return build()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.info("pipeline health component %s failed: %s", key, exc)
        return _pipeline_component(
            key=key,
            label=label,
            status="error",
            latest_at=None,
            age_seconds=None,
            count=None,
            description=description,
            detail=f"Health check failed: {exc.__class__.__name__}",
        )
    except Exception as exc:
        db.rollback()
        logger.exception("pipeline health component %s failed unexpectedly", key)
        return _pipeline_component(
            key=key,
            label=label,
            status="error",
            latest_at=None,
            age_seconds=None,
            count=None,
            description=description,
            detail=f"Health check failed: {exc.__class__.__name__}",
        )


def _pipeline_summary(components: list[dict]) -> dict:
    counts = {
        "healthy": sum(1 for c in components if c.get("status") == "healthy"),
        "stale": sum(1 for c in components if c.get("status") == "stale"),
        "empty": sum(1 for c in components if c.get("status") == "empty"),
        "error": sum(1 for c in components if c.get("status") == "error"),
    }
    total = len(components)
    data_components = [c for c in components if c.get("key") != "api"]
    if counts["error"] == total and total > 0:
        status = "error"
    elif counts["error"] or counts["stale"]:
        status = "degraded"
    elif data_components and all(c.get("status") == "empty" for c in data_components):
        status = "empty"
    elif counts["empty"]:
        status = "degraded"
    else:
        status = "healthy"
    return {**counts, "total": total, "status": status}


def _pipeline_heartbeat_map(db: Session) -> dict[str, PipelineHeartbeat]:
    try:
        return {row.key: row for row in db.query(PipelineHeartbeat).all()}
    except SQLAlchemyError as exc:
        db.rollback()
        logger.debug("pipeline heartbeat rows unavailable: %s", exc)
        return {}


def _component_from_heartbeat(
    heartbeat: PipelineHeartbeat | None,
    *,
    key: str,
    label: str,
    description: str,
    db_latest_at: datetime | None,
    db_count: int | None,
    db_detail: str,
    stale_after: timedelta,
    now: datetime,
) -> dict:
    hb_latest = (
        _latest_datetime(heartbeat.last_heartbeat_at, heartbeat.last_success_at)
        if heartbeat is not None
        else None
    )
    latest_at = _latest_datetime(hb_latest, db_latest_at)
    count = heartbeat.count if heartbeat is not None and heartbeat.count is not None else db_count
    if heartbeat is not None and heartbeat.status == "error":
        status = "error"
    elif hb_latest is not None:
        status = _freshness_status(
            latest_at=hb_latest,
            count=count,
            stale_after=stale_after,
            now=now,
        )
    else:
        status = _freshness_status(
            latest_at=db_latest_at,
            count=db_count,
            stale_after=stale_after,
            now=now,
        )
    detail = heartbeat.detail if heartbeat is not None and heartbeat.detail else db_detail
    if heartbeat is not None and heartbeat.status == "error" and heartbeat.last_error:
        detail = f"{detail} Last error: {heartbeat.last_error}"
    return _pipeline_component(
        key=key,
        label=label,
        status=status,
        latest_at=latest_at,
        age_seconds=_age_seconds(latest_at, now=now),
        count=count,
        description=description,
        detail=detail,
        heartbeat_at=heartbeat.last_heartbeat_at if heartbeat is not None else None,
        last_success_at=heartbeat.last_success_at if heartbeat is not None else None,
        last_error_at=heartbeat.last_error_at if heartbeat is not None else None,
        last_error=heartbeat.last_error if heartbeat is not None else None,
        component_type=heartbeat.component_type if heartbeat is not None else None,
        source="heartbeat" if hb_latest is not None else "db",
        run_id=heartbeat.run_id if heartbeat is not None else None,
    )


def _pipeline_health_payload(db: Session) -> dict:
    now = _utc_now()
    heartbeats = _pipeline_heartbeat_map(db)

    def api_component() -> dict:
        db.execute(text("select 1")).scalar()
        return _pipeline_component(
            key="api",
            label="API",
            status="healthy",
            latest_at=now,
            age_seconds=0,
            count=None,
            description="FastAPI dashboard endpoint and Postgres session.",
            detail="Endpoint responded and the DB session accepted SELECT 1.",
            component_type="process",
            source="request",
        )

    def market_poller_component() -> dict:
        hydrated = db.query(func.count(Market.id)).filter(*_hydrated_market_filters()).scalar() or 0
        unknown = db.query(func.count(Market.id)).filter(Market.status == "unknown").scalar() or 0
        latest_updated, latest_created = db.query(
            func.max(Market.updated_at),
            func.max(Market.created_at),
        ).one()
        latest_at = _latest_datetime(latest_updated, latest_created)
        return _component_from_heartbeat(
            heartbeats.get("market_poller"),
            key="market_poller",
            label="Market poller / hydration",
            description="REST market poller and targeted hydration keeping market metadata filled in.",
            db_latest_at=latest_at,
            db_count=int(hydrated),
            db_detail=f"{int(hydrated):,} hydrated markets; {int(unknown):,} unknown/pending rows.",
            stale_after=_PIPELINE_MARKET_POLLER_STALE_AFTER,
            now=now,
        )

    def ws_trade_component() -> dict:
        count, latest_at = db.query(func.count(Trade.id), func.max(Trade.ts)).one()
        latest_at = _as_utc(latest_at)
        return _component_from_heartbeat(
            heartbeats.get("ws_trade_feed"),
            key="ws_trade_feed",
            label="WebSocket trade feed",
            description="Kalshi WebSocket trade channel writing public executions.",
            db_latest_at=latest_at,
            db_count=int(count or 0),
            db_detail=f"{int(count or 0):,} stored trades; latest trade timestamp drives freshness.",
            stale_after=_PIPELINE_WS_STALE_AFTER,
            now=now,
        )

    def news_ingest_component() -> dict:
        count, latest_seen, latest_published = db.query(
            func.count(NewsArticle.id),
            func.max(NewsArticle.first_seen_at),
            func.max(NewsArticle.published_at),
        ).one()
        latest_at = _latest_datetime(latest_seen, latest_published)
        return _component_from_heartbeat(
            heartbeats.get("news_ingest"),
            key="news_ingest",
            label="News ingest",
            description="Global news/RSS/GDELT ingest storing normalized article metadata.",
            db_latest_at=latest_at,
            db_count=int(count or 0),
            db_detail=f"{int(count or 0):,} normalized articles stored.",
            stale_after=_PIPELINE_DAILY_STALE_AFTER,
            now=now,
        )

    def news_links_component() -> dict:
        count, latest_event = db.query(
            func.count(NewsEvent.id),
            func.max(NewsEvent.created_at),
        ).one()
        latest_article = db.query(func.max(NewsArticle.first_seen_at)).scalar()
        latest_at = _latest_datetime(latest_event, latest_article)
        return _component_from_heartbeat(
            heartbeats.get("news_links"),
            key="news_links",
            label="News links",
            description="Candidate article-to-market links from relevance scoring.",
            db_latest_at=latest_at,
            db_count=int(count or 0),
            db_detail=f"{int(count or 0):,} linked news events.",
            stale_after=_PIPELINE_DAILY_STALE_AFTER,
            now=now,
        )

    def news_trade_correlations_component() -> dict:
        count, latest_at = (
            db.query(func.count(NewsEvent.id), func.max(NewsEvent.created_at))
            .filter(NewsEvent.pre_news_trade_score > 0)
            .one()
        )
        return _component_from_heartbeat(
            heartbeats.get("news_trade_correlations"),
            key="news_trade_correlations",
            label="News/trade correlations",
            description="Materialized pre-news trade alignment on linked news events.",
            db_latest_at=latest_at,
            db_count=int(count or 0),
            db_detail=f"{int(count or 0):,} links have a positive pre-news trade score.",
            stale_after=_PIPELINE_DAILY_STALE_AFTER,
            now=now,
        )

    def trade_flags_component() -> dict:
        count, latest_ts, latest_created = db.query(
            func.count(TradeFlag.id),
            func.max(TradeFlag.ts),
            func.max(TradeFlag.created_at),
        ).one()
        latest_at = _latest_datetime(latest_ts, latest_created)
        return _component_from_heartbeat(
            heartbeats.get("trade_flags"),
            key="trade_flags",
            label="Trade flags",
            description="Contextual suspicious-trade flag materializer.",
            db_latest_at=latest_at,
            db_count=int(count or 0),
            db_detail=f"{int(count or 0):,} persisted trade flags.",
            stale_after=_PIPELINE_DAILY_STALE_AFTER,
            now=now,
        )

    def quote_book_anomalies_component() -> dict:
        count, latest_at = db.query(func.count(Anomaly.id), func.max(Anomaly.created_at)).one()
        return _component_from_heartbeat(
            heartbeats.get("quote_book_anomalies"),
            key="quote_book_anomalies",
            label="Quote/book alerts",
            description="Quote and order-book market-state alert materialization.",
            db_latest_at=latest_at,
            db_count=int(count or 0),
            db_detail=f"{int(count or 0):,} stored quote/book alert rows.",
            stale_after=_PIPELINE_DAILY_STALE_AFTER,
            now=now,
        )

    def retention_projection_component() -> dict:
        count, latest_at = db.query(
            func.count(MarketMetric.market_pk),
            func.max(MarketMetric.updated_at),
        ).one()
        promoted_count: int | None = None
        try:
            promoted_count = (
                db.query(func.count(MarketMetric.market_pk))
                .filter(
                    or_(
                        MarketMetric.storage_tier != "observe_only",
                        MarketMetric.retention_score > 0,
                    )
                )
                .scalar()
                or 0
            )
        except SQLAlchemyError:
            db.rollback()
        detail = f"{int(count or 0):,} market metric rows"
        if promoted_count is not None:
            detail += f"; {int(promoted_count):,} promoted above observe-only."
        else:
            detail += "; retention tier detail unavailable."
        return _component_from_heartbeat(
            heartbeats.get("retention_projection"),
            key="retention_projection",
            label="Retention/storage-tier projection",
            description="MarketMetric projection carrying retention tier and compact serving state.",
            db_latest_at=latest_at,
            db_count=int(count or 0),
            db_detail=detail,
            stale_after=_PIPELINE_DAILY_STALE_AFTER,
            now=now,
        )

    def storage_guardrails_component() -> dict:
        snapshot = storage_health_snapshot(db, table_limit=5)
        status = snapshot.get("summary", {}).get("status") or "empty"
        if status not in {"healthy", "stale", "empty", "error"}:
            status = "stale"
        heartbeat = heartbeats.get("storage_guardrails")
        if heartbeat is not None and heartbeat.status == "error":
            return _component_from_heartbeat(
                heartbeat,
                key="storage_guardrails",
                label="Storage guardrails",
                description=(
                    "Postgres, local disk, ClickHouse, and search-index storage pressure."
                ),
                db_latest_at=now,
                db_count=None,
                db_detail=storage_health_detail(snapshot),
                stale_after=_PIPELINE_DAILY_STALE_AFTER,
                now=now,
            )
        return _pipeline_component(
            key="storage_guardrails",
            label="Storage guardrails",
            status=status,
            latest_at=now,
            age_seconds=0,
            count=None,
            description=(
                "Postgres, local disk, ClickHouse, and search-index storage pressure."
            ),
            detail=storage_health_detail(snapshot),
            component_type="projection",
            source="db",
        )

    component_builders = [
        (
            "api",
            "API",
            "FastAPI dashboard endpoint and Postgres session.",
            api_component,
        ),
        (
            "market_poller",
            "Market poller / hydration",
            "REST market poller and targeted hydration keeping market metadata filled in.",
            market_poller_component,
        ),
        (
            "ws_trade_feed",
            "WebSocket trade feed",
            "Kalshi WebSocket trade channel writing public executions.",
            ws_trade_component,
        ),
        (
            "news_ingest",
            "News ingest",
            "Global news/RSS/GDELT ingest storing normalized article metadata.",
            news_ingest_component,
        ),
        (
            "news_links",
            "News links",
            "Candidate article-to-market links from relevance scoring.",
            news_links_component,
        ),
        (
            "news_trade_correlations",
            "News/trade correlations",
            "Materialized pre-news trade alignment on linked news events.",
            news_trade_correlations_component,
        ),
        (
            "trade_flags",
            "Trade flags",
            "Contextual suspicious-trade flag materializer.",
            trade_flags_component,
        ),
        (
            "quote_book_anomalies",
            "Quote/book alerts",
            "Quote and order-book market-state alert materialization.",
            quote_book_anomalies_component,
        ),
        (
            "retention_projection",
            "Retention/storage-tier projection",
            "MarketMetric projection carrying retention tier and compact serving state.",
            retention_projection_component,
        ),
        (
            "storage_guardrails",
            "Storage guardrails",
            "Postgres, local disk, ClickHouse, and search-index storage pressure.",
            storage_guardrails_component,
        ),
    ]
    components = [
        _safe_pipeline_component(
            db,
            key=key,
            label=label,
            description=description,
            build=build,
        )
        for key, label, description, build in component_builders
    ]
    return {
        "generated_at": now.isoformat(),
        "summary": _pipeline_summary(components),
        "components": components,
    }


# --- /stats and /breakdown --------------------------------------------------


def _estimated_table_count(db: Session, table_name: str) -> int:
    """Fast approximate row count from Postgres planner stats.

    Multi-million-row exact counts are a poor fit for the overview header:
    they block first paint, and the values change continuously under ingest.
    `reltuples` is maintained by VACUUM/ANALYZE and is close enough for a
    live dashboard counter.
    """
    estimate = db.execute(
        text(
            "select reltuples::bigint from pg_class where oid = to_regclass(:table_name)"
        ),
        {"table_name": table_name},
    ).scalar()
    return max(0, int(estimate or 0))


def _market_metrics_available(db: Session) -> bool:
    """True when the compact market read model has been populated."""
    return bool(db.query(MarketMetric.market_pk).limit(1).scalar() is not None)


def _metric_latest_values_for_market_pks(
    db: Session, market_pks: list[int]
) -> dict[int, dict[str, float | int | str | None]]:
    if not market_pks:
        return {}
    rows = (
        db.query(MarketMetric)
        .filter(MarketMetric.market_pk.in_(market_pks))
        .all()
    )
    return {
        int(row.market_pk): {
            "last_price": float(row.last_price_cents) / 100.0
            if row.last_price_cents is not None
            else None,
            "volume_24h": float(row.volume_24h_contracts)
            if row.volume_24h_contracts is not None
            else None,
            "trade_count": int(row.trade_count or 0),
            "anomaly_count": int(row.anomaly_count or 0),
            "evidence_score": float(row.evidence_score or 0.0),
            "urgency_score": float(row.urgency_score or 0.0),
            "storage_tier": row.storage_tier,
            "retention_score": int(row.retention_score or 0),
        }
        for row in rows
    }


def _hydrated_market_filters() -> list:
    return [
        Market.status.notin_(("unknown", "out_of_scope")),
        Market.title != Market.market_id,
    ]


def _news_link_market_filters() -> list:
    return [
        Market.status != "unknown",
        Market.title != Market.market_id,
        or_(Market.category.is_(None), Market.category != "exotic_combo"),
    ]


def _stats_payload(db: Session, *, market_scope: str = "active") -> dict:
    """Coarse system-wide counts. Used by `GET /stats` and `GET /overview`."""
    market_scope = _normalize_market_scope(market_scope)
    hydrated = _hydrated_market_filters()
    scoped = hydrated + _market_scope_filters(market_scope)
    markets = db.query(func.count(Market.id)).filter(*scoped).scalar() or 0
    markets_all = db.query(func.count(Market.id)).filter(*hydrated).scalar() or 0
    markets_active = (
        db.query(func.count(Market.id))
        .filter(*(hydrated + _market_scope_filters("active")))
        .scalar()
        or 0
    )
    markets_historical = (
        db.query(func.count(Market.id))
        .filter(*(hydrated + _market_scope_filters("historical")))
        .scalar()
        or 0
    )
    markets_unknown = (
        db.query(func.count(Market.id)).filter(Market.status == "unknown").scalar() or 0
    )
    markets_high_prior = (
        db.query(func.count(Market.id))
        .filter(*scoped)
        .filter(Market.manipulability_prior.in_(("high", "medium_high")))
        .scalar()
        or 0
    )
    metric_projection = _market_metrics_available(db)
    metric_counts = None
    if metric_projection:
        metric_counts = (
            db.query(
                func.coalesce(func.sum(MarketMetric.trade_count), 0).label("trades"),
                func.coalesce(func.sum(MarketMetric.anomaly_count), 0).label(
                    "anomalies"
                ),
                func.coalesce(func.sum(MarketMetric.high_anomaly_count), 0).label(
                    "high_anomalies"
                ),
                func.count(
                    case((MarketMetric.anomaly_count > 0, MarketMetric.market_pk))
                ).label("markets_with_flags"),
            )
            .join(Market, Market.id == MarketMetric.market_pk)
            .filter(*scoped)
            .one()
        )
    trades = (
        int(metric_counts.trades)
        if metric_counts is not None and int(metric_counts.trades or 0) > 0
        else _estimated_table_count(db, "trades")
    )
    snapshots = _estimated_table_count(db, "market_snapshots")
    book_events = _estimated_table_count(db, "book_events")
    anomalies = (
        int(metric_counts.anomalies)
        if metric_counts is not None and int(metric_counts.anomalies or 0) > 0
        else _estimated_table_count(db, "anomalies")
    )
    news_articles = db.query(func.count(NewsArticle.id)).scalar() or 0
    anomalies_high = (
        int(metric_counts.high_anomalies)
        if metric_counts is not None and int(metric_counts.high_anomalies or 0) > 0
        else db.query(func.count(Anomaly.id)).filter(Anomaly.severity == "high").scalar()
        or 0
    )
    markets_with_flags = (
        int(metric_counts.markets_with_flags)
        if metric_counts is not None
        else min(markets, anomalies)
    )

    return {
        "market_scope": market_scope,
        "markets": markets,
        "markets_all": markets_all,
        "markets_active": markets_active,
        "markets_historical": markets_historical,
        "markets_status_unknown": markets_unknown,
        "markets_high_prior": markets_high_prior,
        "markets_with_flags": markets_with_flags,
        "trades": trades,
        "snapshots": snapshots,
        "book_events": book_events,
        "anomalies": anomalies,
        "news_articles": int(news_articles),
        "anomalies_high_severity": anomalies_high,
    }


def _breakdown_payload(db: Session, *, market_scope: str = "active") -> dict:
    """Classification pivots for the overview page."""
    market_scope = _normalize_market_scope(market_scope)

    hydrated = _hydrated_market_filters() + _market_scope_filters(market_scope)

    def _group_count(col):
        rows = (
            db.query(
                func.coalesce(col, "unclassified").label("k"), func.count(Market.id)
            )
            .filter(*hydrated)
            .group_by("k")
            .order_by(func.count(Market.id).desc())
            .all()
        )
        return [{"key": r[0], "count": int(r[1])} for r in rows]

    by_category = _group_count(Market.category)
    by_prior = _group_count(Market.manipulability_prior)
    by_confidence = _group_count(Market.classifier_confidence)
    by_layer = _group_count(Market.classifier_layer)

    cross_rows = (
        db.query(
            func.coalesce(Market.category, "unclassified").label("cat"),
            func.coalesce(Market.manipulability_prior, "unclassified").label("prior"),
            func.count(Market.id).label("c"),
        )
        .filter(*hydrated)
        .group_by("cat", "prior")
        .order_by(desc("c"))
        .all()
    )
    category_x_prior = [
        {"category": r.cat, "prior": r.prior, "count": int(r.c)} for r in cross_rows
    ]

    return {
        "by_category": by_category,
        "by_prior": by_prior,
        "by_confidence": by_confidence,
        "by_layer": by_layer,
        "category_x_prior": category_x_prior,
    }


@router.get("/stats")
def get_stats(
    market_scope: str = Query(default="active", description="active | historical | all"),
    db: Session = Depends(get_db),
) -> dict:
    """Coarse system-wide counts. Drives the overview header."""
    return _cached_dashboard_payload(
        f"stats:{market_scope}", lambda: _stats_payload(db, market_scope=market_scope)
    )


@router.get("/breakdown")
def get_breakdown(
    market_scope: str = Query(default="active", description="active | historical | all"),
    db: Session = Depends(get_db),
) -> dict:
    """Classification pivots for the overview page.

    Returns counts grouped by each of the four classifier dimensions
    (`category`, `manipulability_prior`, `classifier_confidence`,
    `classifier_layer`) plus a category × prior cross-tab for the
    "where's the high-prior cluster?" question. NULLs are bucketed as
    `"unclassified"` so the frontend can render them without special
    casing.
    """
    return _cached_dashboard_payload(
        f"breakdown:{market_scope}",
        lambda: _breakdown_payload(db, market_scope=market_scope),
    )


@router.get("/pipeline-health")
def get_pipeline_health(db: Session = Depends(get_db)) -> dict:
    """Freshness and row-count health for dashboard pipeline components.

    These are not all standalone services: some are long-running processes,
    while others are jobs, materializers, or compact DB projections.
    """
    return _pipeline_health_payload(db)


def _news_diagnostics_payload(db: Session) -> dict:
    now = _utc_now()
    heartbeats = _pipeline_heartbeat_map(db)
    ingest = heartbeats.get("news_ingest")
    links = heartbeats.get("news_links")
    correlations = heartbeats.get("news_trade_correlations")
    metadata = ingest.metadata_json if ingest is not None and ingest.metadata_json else {}
    source_counts = metadata.get("source_counts") if isinstance(metadata, dict) else None
    if not isinstance(source_counts, dict):
        source_counts = {}
    registry = metadata.get("source_registry") if isinstance(metadata, dict) else None
    if not isinstance(registry, dict):
        registry = source_registry_diagnostics()

    article_count, latest_seen, latest_published = db.query(
        func.count(NewsArticle.id),
        func.max(NewsArticle.first_seen_at),
        func.max(NewsArticle.published_at),
    ).one()
    link_count, correlated_count, latest_link = (
        db.query(
            func.count(NewsEvent.id),
            func.sum(case((NewsEvent.pre_news_trade_score > 0, 1), else_=0)),
            func.max(NewsEvent.created_at),
        ).one()
    )

    rss_feed_counts = source_counts.get("rss_feed_counts") or {}
    rss_feed_details = source_counts.get("rss_feed_details") or []
    if not rss_feed_details and isinstance(rss_feed_counts, dict):
        rss_feed_details = [
            {"source": source, "label": source, "count": count, "error": None}
            for source, count in rss_feed_counts.items()
        ]
    if isinstance(rss_feed_details, list):
        rss_feed_details = sorted(
            [d for d in rss_feed_details if isinstance(d, dict)],
            key=lambda d: int(d.get("count") or 0),
            reverse=True,
        )
    else:
        rss_feed_details = []

    latest_at = _latest_datetime(latest_seen, latest_published, latest_link)
    return {
        "generated_at": now.isoformat(),
        "latest_at": latest_at.isoformat() if latest_at else None,
        "age_seconds": _age_seconds(latest_at, now=now),
        "provider_status": metadata.get("provider_status")
        if isinstance(metadata, dict)
        else None,
        "fetch_error": metadata.get("fetch_error") if isinstance(metadata, dict) else None,
        "summary": {
            "articles_stored": int(article_count or 0),
            "articles_seen_last_run": int(metadata.get("articles_seen") or 0)
            if isinstance(metadata, dict)
            else 0,
            "articles_upserted_last_run": int(metadata.get("articles_upserted") or 0)
            if isinstance(metadata, dict)
            else 0,
            "article_clusters_seen_last_run": int(
                metadata.get("article_clusters_seen") or 0
            )
            if isinstance(metadata, dict)
            else 0,
            "news_events_linked": int(link_count or 0),
            "news_events_linked_last_run": int(
                metadata.get("news_events_linked") or 0
            )
            if isinstance(metadata, dict)
            else 0,
            "positive_correlations": int(correlated_count or 0),
            "profiles_refreshed_last_run": int(
                metadata.get("profiles_refreshed") or 0
            )
            if isinstance(metadata, dict)
            else 0,
        },
        "source_counts": source_counts,
        "rss_feed_details": rss_feed_details,
        "source_registry": registry,
        "heartbeats": {
            "news_ingest": _component_from_heartbeat(
                ingest,
                key="news_ingest",
                label="News ingest",
                description="Global article collection.",
                db_latest_at=latest_at,
                db_count=int(article_count or 0),
                db_detail=f"{int(article_count or 0):,} stored articles.",
                stale_after=_PIPELINE_DAILY_STALE_AFTER,
                now=now,
            ),
            "news_links": _component_from_heartbeat(
                links,
                key="news_links",
                label="News links",
                description="Article-to-market relevance links.",
                db_latest_at=latest_link,
                db_count=int(link_count or 0),
                db_detail=f"{int(link_count or 0):,} linked news events.",
                stale_after=_PIPELINE_DAILY_STALE_AFTER,
                now=now,
            ),
            "news_trade_correlations": _component_from_heartbeat(
                correlations,
                key="news_trade_correlations",
                label="News/trade correlations",
                description="Pre-news trade alignment materializer.",
                db_latest_at=latest_link,
                db_count=int(correlated_count or 0),
                db_detail=f"{int(correlated_count or 0):,} positive correlations.",
                stale_after=_PIPELINE_DAILY_STALE_AFTER,
                now=now,
            ),
        },
    }


@router.get("/news-diagnostics")
def get_news_diagnostics(db: Session = Depends(get_db)) -> dict:
    """Latest news ingest diagnostics from DB counts and heartbeat metadata."""
    return _news_diagnostics_payload(db)


@router.get("/historical-signal-qa")
def get_historical_signal_qa(
    limit: int = Query(default=8, ge=1, le=50),
    min_flag_score: float = Query(default=5.0, ge=0.0, le=10.0),
    category: str | None = Query(default=None),
    market_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    """Historical post-mortem sample for checking whether flags look useful."""
    return historical_signal_report(
        db,
        limit=limit,
        min_flag_score=min_flag_score,
        category=category,
        market_id=market_id,
    )


@router.get("/storage-health")
def get_storage_health(
    table_limit: int = Query(default=8, ge=1, le=25),
    db: Session = Depends(get_db),
) -> dict:
    """Detailed storage pressure and largest-table snapshot."""
    return storage_health_snapshot(db, table_limit=table_limit)


@router.get("/search")
def search_dashboard(
    q: str = Query(default="", description="Market/news search text."),
    scope: str = Query(default="all", description="all | markets | news"),
    limit: int = Query(default=10, ge=1, le=25),
    db: Session = Depends(get_db),
) -> dict:
    """Search markets and stored news, using OpenSearch with DB fallback."""
    query = (q or "").strip()
    if not query:
        return {
            "query": "",
            "scope": scope,
            "provider": "empty",
            "took_ms": 0,
            "markets": [],
            "news": [],
            "suggestions": [],
        }
    return dashboard_search(db, query, scope=scope, limit=limit)


def _top_markets_payload(db: Session, limit: int, *, market_scope: str = "active") -> dict:
    if _market_metrics_available(db):
        rows = (
            db.query(Market, MarketMetric)
            .join(MarketMetric, MarketMetric.market_pk == Market.id)
            .filter(*(_hydrated_market_filters() + _market_scope_filters(market_scope)))
            .filter(MarketMetric.trade_count > 0)
            .order_by(MarketMetric.trade_count.desc(), MarketMetric.last_trade_ts.desc())
            .limit(limit)
            .all()
        )
        pks = [int(market.id) for market, _metric in rows]
        latest_by_pk = _latest_snapshot_values_for_market_pks(db, pks)
        trade_dollars: dict[int, float] = {}
        if pks:
            trade_price = case(
                (
                    func.lower(func.coalesce(Trade.taker_side, "")) == "no",
                    Trade.no_price_dollars,
                ),
                else_=Trade.yes_price_dollars,
            )
            trade_dollars = {
                int(market_pk): float(dollars or 0.0)
                for market_pk, dollars in (
                    db.query(
                        Trade.market_pk,
                        func.sum(
                            func.coalesce(Trade.count_fp, 0)
                            * func.coalesce(trade_price, 0)
                        ).label("trade_dollar_volume"),
                    )
                    .filter(Trade.market_pk.in_(pks))
                    .group_by(Trade.market_pk)
                    .all()
                )
            }
        return {
            "count": len(rows),
            "markets": [
                _serialize_market_row(
                    market,
                    trade_count=int(metric.trade_count or 0),
                    anomaly_count=int(metric.anomaly_count or 0),
                    last_price=(
                        float(metric.last_price_cents) / 100.0
                        if metric.last_price_cents is not None
                        else latest_by_pk.get(market.id, {}).get("last_price")
                    ),
                    volume_24h=(
                        float(metric.volume_24h_contracts)
                        if metric.volume_24h_contracts is not None
                        else latest_by_pk.get(market.id, {}).get("volume_24h")
                    ),
                    trade_dollar_volume=trade_dollars.get(market.id),
                    evidence_score=float(metric.evidence_score or 0.0),
                    urgency_score=float(metric.urgency_score or 0.0),
                    storage_tier=metric.storage_tier,
                    retention_score=int(metric.retention_score or 0),
                )
                for market, metric in rows
            ],
        }

    recent_trades = (
        select(
            Trade.market_pk,
            Trade.ts,
            Trade.id,
            Trade.count_fp,
            Trade.yes_price_dollars,
            Trade.no_price_dollars,
            Trade.taker_side,
        )
        .join(Market, Market.id == Trade.market_pk)
        .where(*(_hydrated_market_filters() + _market_scope_filters(market_scope)))
        .order_by(Trade.ts.desc(), Trade.id.desc())
        .limit(_TOP_MARKETS_RECENT_TRADE_SAMPLE)
        .subquery()
    )
    trade_price = case(
        (
            func.lower(func.coalesce(recent_trades.c.taker_side, "")) == "no",
            recent_trades.c.no_price_dollars,
        ),
        else_=recent_trades.c.yes_price_dollars,
    )
    trade_rows = (
        db.query(
            recent_trades.c.market_pk,
            func.count().label("trade_count"),
            func.sum(
                func.coalesce(recent_trades.c.count_fp, 0)
                * func.coalesce(trade_price, 0)
            ).label("trade_dollar_volume"),
        )
        .group_by(recent_trades.c.market_pk)
        .order_by(func.count().desc())
        .limit(limit)
        .all()
    )

    pks = [int(r.market_pk) for r in trade_rows]
    if not pks:
        return {"count": 0, "markets": []}

    trade_counts = {int(r.market_pk): int(r.trade_count or 0) for r in trade_rows}
    trade_dollars = {
        int(r.market_pk): float(r.trade_dollar_volume or 0.0) for r in trade_rows
    }
    markets_by_pk = {m.id: m for m in db.query(Market).filter(Market.id.in_(pks)).all()}
    latest_by_pk = _latest_snapshot_values_for_market_pks(db, pks)

    rows = [markets_by_pk[pk] for pk in pks if pk in markets_by_pk]
    return {
        "count": len(rows),
        "markets": [
            _serialize_market_row(
                market,
                trade_count=trade_counts.get(market.id, 0),
                last_price=latest_by_pk.get(market.id, {}).get("last_price"),
                volume_24h=latest_by_pk.get(market.id, {}).get("volume_24h"),
                trade_dollar_volume=trade_dollars.get(market.id),
            )
            for market in rows
        ],
    }


def _recent_anomalies_payload(
    db: Session,
    limit: int,
    severity: str | None,
    *,
    market_scope: str = "active",
) -> dict:
    base = db.query(Anomaly, Market).join(Market, Market.id == Anomaly.market_pk)
    base = base.filter(*(_hydrated_market_filters() + _market_scope_filters(market_scope)))
    if severity:
        base = base.filter(Anomaly.severity == severity)
    rows = (
        base.order_by(Anomaly.created_at.desc(), Anomaly.id.desc()).limit(limit).all()
    )
    return {
        "count": len(rows),
        "anomalies": [
            {
                "id": a.id,
                "market_id": m.market_id,
                "title": m.title,
                "subtitle": m.subtitle,
                "category": m.category,
                "manipulability_prior": m.manipulability_prior,
                "score": float(a.score),
                "severity": a.severity,
                "reasons": a.reasons,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a, m in rows
        ],
    }


def _components_dict(event: NewsEvent) -> dict:
    return event.score_components if isinstance(event.score_components, dict) else {}


def _is_weak_factor_only_news_event(event: NewsEvent) -> bool:
    components = _components_dict(event)
    direction = components.get("market_direction")
    candidate = components.get("candidate_generation")
    if not isinstance(direction, dict) or not isinstance(candidate, dict):
        return False
    if direction.get("label") != "ambiguous":
        return False
    direct_scores = (
        "lexical_relevance",
        "entity_relevance",
        "alias_relevance",
    )
    if any(float(components.get(key) or 0.0) > 0 for key in direct_scores):
        return False
    reasons = candidate.get("candidate_reasons")
    if not isinstance(reasons, list) or reasons != ["category_factor"]:
        return False
    return float(components.get("factor_relevance") or 0.0) > 0


def _news_relevance_component_payload(components: dict) -> dict:
    keys = (
        "lexical_relevance",
        "entity_relevance",
        "alias_relevance",
        "factor_relevance",
        "factor_hits",
        "direction_hint",
        "scorer",
    )
    return {key: components.get(key) for key in keys if key in components}


def _news_candidate_generation_payload(components: dict) -> dict:
    candidate = components.get("candidate_generation")
    return candidate if isinstance(candidate, dict) else {}


def _linked_news_article_payload(
    event: NewsEvent,
    article: NewsArticle,
    *,
    include_components: bool = True,
    components_override: dict | None = None,
    relevance_score_override: float | None = None,
) -> dict:
    components = _components_dict(event)
    if components_override is not None:
        components = {**components, **components_override}
    direction = components.get("market_direction")
    if not isinstance(direction, dict):
        direction = {}
    correlation = components.get("news_trade_correlation")
    if not isinstance(correlation, dict):
        correlation = {}
    payload = {
        "event_id": event.id,
        "article_id": article.id,
        "title": article.title,
        "url": article.canonical_url,
        "source": article.domain,
        "language": article.language,
        "published_at": article.published_at.isoformat()
        if article.published_at
        else None,
        "first_seen_at": article.first_seen_at.isoformat()
        if article.first_seen_at
        else None,
        "tone": None,
        "relevance_score": float(
            relevance_score_override
            if relevance_score_override is not None
            else event.relevance_score
            or 0.0
        ),
        "pre_news_trade_score": float(event.pre_news_trade_score or 0.0),
        "status": event.status,
        "leakage_window_seconds": event.leakage_window_seconds,
        "market_direction": direction,
        "direction_label": direction.get("label"),
        "direction_confidence": direction.get("confidence"),
        "reasons": correlation.get("reasons") or [],
        "best_trade": correlation.get("best_trade"),
        "relevance_components": _news_relevance_component_payload(components),
        "candidate_generation": _news_candidate_generation_payload(components),
    }
    if include_components:
        payload["news_trade_correlation"] = correlation
    return payload


def _current_news_link_for_market(
    article: NewsArticle,
    market: Market,
    *,
    min_relevance: float = 0.35,
) -> tuple[float, dict] | None:
    """Re-score stored news links against today's profile rules.

    News links are derived materialization.  If profile rules improve, old
    links can become stale; market-detail pages should not keep surfacing those
    stale links just because the row still exists.
    """

    try:
        profile = MarketNewsProfile(**profile_for_market(market))
        relevance = hybrid_news_relevance(article, profile)
        if relevance.score < min_relevance:
            return None
        components = components_with_market_direction(
            article,
            profile,
            relevance.components,
        )
        return relevance.score, components
    except Exception as exc:  # pragma: no cover - defensive read-path fallback
        logger.info(
            "current news relevance check failed for %s: %s",
            market.market_id,
            exc,
        )
        return None


def _news_signal_payload(
    event: NewsEvent,
    article: NewsArticle,
    market: Market,
    *,
    components_override: dict | None = None,
    relevance_score_override: float | None = None,
) -> dict:
    article_payload = _linked_news_article_payload(
        event,
        article,
        components_override=components_override,
        relevance_score_override=relevance_score_override,
    )
    return {
        "event_id": event.id,
        "market_id": market.market_id,
        "event_market_id": market.event_id,
        "title": market.title,
        "subtitle": market.subtitle,
        "category": market.category,
        "manipulability_prior": market.manipulability_prior,
        "article": article_payload,
        "article_title": article.title,
        "article_url": article.canonical_url,
        "article_source": article.domain,
        "first_seen_at": article_payload["first_seen_at"],
        "relevance_score": article_payload["relevance_score"],
        "pre_news_trade_score": article_payload["pre_news_trade_score"],
        "status": event.status,
        "leakage_window_seconds": event.leakage_window_seconds,
        "direction_label": article_payload["direction_label"],
        "direction_confidence": article_payload["direction_confidence"],
        "reasons": article_payload["reasons"],
        "best_trade": article_payload["best_trade"],
    }


def _news_signals_payload(
    db: Session,
    *,
    limit: int,
    min_score: float,
    status: str | None = None,
    include_ambiguous: bool = False,
    market_scope: str = "active",
) -> dict:
    q = (
        db.query(NewsEvent, NewsArticle, Market)
        .join(NewsArticle, NewsArticle.id == NewsEvent.article_id)
        .join(Market, Market.id == NewsEvent.market_pk)
        .filter(*(_news_link_market_filters() + _market_scope_filters(market_scope)))
        .filter(NewsEvent.pre_news_trade_score >= min_score)
    )
    if status:
        q = q.filter(NewsEvent.status == status)
    scan_limit = limit if include_ambiguous else max(limit * 100, 500)
    rows = (
        q.order_by(
            NewsEvent.pre_news_trade_score.desc(),
            NewsArticle.first_seen_at.desc(),
            NewsEvent.id.desc(),
        )
        .limit(scan_limit)
        .all()
    )
    signals: list[dict] = []
    seen_article_event_groups: set[tuple[int | None, str]] = set()
    for event, article, market in rows:
        if _is_weak_factor_only_news_event(event):
            continue
        current_link = _current_news_link_for_market(article, market)
        if current_link is None:
            continue
        current_relevance, current_components = current_link
        payload = _news_signal_payload(
            event,
            article,
            market,
            components_override=current_components,
            relevance_score_override=current_relevance,
        )
        if (
            not include_ambiguous
            and payload["direction_label"] not in {"supports_yes", "supports_no"}
        ):
            continue
        event_group = market.event_id or market.market_id or str(market.id)
        dedupe_key = (article.id, event_group)
        if dedupe_key in seen_article_event_groups:
            continue
        seen_article_event_groups.add(dedupe_key)
        signals.append(payload)
        if len(signals) >= limit:
            break
    return {
        "count": len(signals),
        "min_score": min_score,
        "signals": signals,
    }


def _suspicious_trades_payload(
    db: Session,
    *,
    limit: int,
    sample: int = _SUSPICIOUS_TRADE_SAMPLE,
    market_scope: str = "active",
) -> dict:
    scope_filters = _market_scope_filters(market_scope)
    persisted = (
        db.query(TradeFlag, Trade, Market)
        .join(Trade, Trade.id == TradeFlag.trade_pk)
        .join(Market, Market.id == TradeFlag.market_pk)
        .filter(*(_hydrated_market_filters() + scope_filters))
        .order_by(TradeFlag.score.desc(), TradeFlag.ts.desc())
        .limit(limit)
        .all()
    )
    if persisted:
        return {
            "count": len(persisted),
            "trades": [
                {
                    "market_id": market.market_id,
                    "event_id": market.event_id,
                    "title": market.title,
                    "subtitle": market.subtitle,
                    "category": market.category,
                    "manipulability_prior": market.manipulability_prior,
                    "trade_id": trade.trade_id,
                    "ts": trade.ts.isoformat() if trade.ts else None,
                    "yes_price": float(trade.yes_price_dollars)
                    if trade.yes_price_dollars is not None
                    else None,
                    "no_price": float(trade.no_price_dollars)
                    if trade.no_price_dollars is not None
                    else None,
                    "count": float(trade.count_fp)
                    if trade.count_fp is not None
                    else None,
                    "trade_dollar_amount": _trade_notional_dollars(
                        yes_price=trade.yes_price_dollars,
                        no_price=trade.no_price_dollars,
                        count=trade.count_fp,
                        taker_side=trade.taker_side,
                    ),
                    "taker_side": trade.taker_side,
                    "suspicion": float(flag.score),
                    "local_suspicion": float(flag.local_score),
                    "context_score": float(flag.context_score),
                    "reasons": flag.reasons or [],
                    "features": {
                        "context": flag.features or {},
                        "components": flag.components or {},
                    },
                    "severity": flag.severity,
                    "promoted_storage_tier": flag.promoted_storage_tier,
                }
                for flag, trade, market in persisted
            ],
            "sample": sample,
            "source": "trade_flags",
        }

    rows = (
        db.query(Trade, Market)
        .join(Market, Market.id == Trade.market_pk)
        .filter(*(_hydrated_market_filters() + scope_filters))
        .order_by(Trade.ts.desc(), Trade.id.desc())
        .limit(sample)
        .all()
    )

    by_market: dict[int, list[tuple[Trade, Market]]] = defaultdict(list)
    peer_rows: list[dict] = []
    for trade, market in rows:
        by_market[market.id].append((trade, market))
        peer_rows.append(
            {
                "market_pk": trade.market_pk,
                "category": market.category,
                "subcategory": market.subcategory,
                "ts": trade.ts.isoformat() if trade.ts else None,
                "yes_price": float(trade.yes_price_dollars)
                if trade.yes_price_dollars is not None
                else None,
                "count": float(trade.count_fp) if trade.count_fp is not None else None,
            }
        )
    peer_baselines = build_peer_baselines(list(reversed(peer_rows)), min_points=12)

    candidates: list[dict] = []
    for market_rows in by_market.values():
        market_rows = sorted(market_rows, key=lambda x: (x[0].ts, x[0].id))
        market = market_rows[0][1]
        payloads = [
            {
                "ts": t.ts.isoformat() if t.ts else None,
                "yes_price": float(t.yes_price_dollars)
                if t.yes_price_dollars is not None
                else None,
                "no_price": float(t.no_price_dollars)
                if t.no_price_dollars is not None
                else None,
                "count": float(t.count_fp) if t.count_fp is not None else None,
                "trade_dollar_amount": _trade_notional_dollars(
                    yes_price=t.yes_price_dollars,
                    no_price=t.no_price_dollars,
                    count=t.count_fp,
                    taker_side=t.taker_side,
                ),
                "taker_side": t.taker_side,
            }
            for t, _m in market_rows
        ]
        explanations = explain_trades_against_window(payloads, window=50)
        contextual = explain_trades_with_context(
            payloads,
            market=_market_context(market),
            local_explanations=explanations,
            peer_baseline=peer_baselines.get(
                (str(market.category or "unclassified"), str(market.subcategory or "*"))
            )
            or peer_baselines.get((str(market.category or "unclassified"), "*")),
        )
        for (trade, market), payload, explanation, context in zip(
            market_rows, payloads, explanations, contextual
        ):
            if explanation is None and context is None:
                continue
            local_score = float(explanation["score"]) if explanation else 0.0
            context_score = float(context["score"]) if context else 0.0
            score = max(local_score, context_score)
            if score <= 0:
                continue
            candidates.append(
                {
                    "market_id": market.market_id,
                    "event_id": market.event_id,
                    "title": market.title,
                    "subtitle": market.subtitle,
                    "category": market.category,
                    "manipulability_prior": market.manipulability_prior,
                    "trade_id": trade.trade_id,
                    **payload,
                    "suspicion": score,
                    "local_suspicion": local_score,
                    "context_score": context_score,
                    "reasons": sorted(
                        set(
                            (explanation or {}).get("reasons", [])
                            + (context or {}).get("reasons", [])
                        )
                    ),
                    "features": {
                        **((explanation or {}).get("features", {})),
                        "context": (context or {}).get("features", {}),
                        "components": (context or {}).get("components", {}),
                    },
                }
            )

    candidates.sort(key=lambda x: (x["suspicion"], x["ts"] or ""), reverse=True)
    return {
        "count": min(len(candidates), limit),
        "trades": candidates[:limit],
        "sample": sample,
    }


@router.get("/overview")
def get_dashboard_overview(
    top: int = Query(default=15, ge=1, le=50),
    anomalies: int = Query(default=20, ge=1, le=100),
    market_scope: str = Query(default="active", description="active | historical | all"),
    severity: str | None = Query(
        default=None,
        description="If set, filter recent-anomalies in this bundle the same as `GET /anomalies`.",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """One round-trip for the home page: stats, charts, top markets, recent flags.

    Cuts TTFB vs four separate fetches; still runs the same SQL as those routes.
    """
    cache_key = f"overview:{top}:{anomalies}:{severity or ''}:{market_scope}"
    return _cached_dashboard_payload(
        cache_key,
        lambda: {
            "stats": _stats_payload(db, market_scope=market_scope),
            "breakdown": _breakdown_payload(db, market_scope=market_scope),
            "top_markets": _top_markets_payload(db, top, market_scope="active"),
            "recent_anomalies": _recent_anomalies_payload(
                db, anomalies, severity, market_scope="active"
            ),
            "suspicious_trades": _suspicious_trades_payload(
                db, limit=12, market_scope=market_scope
            ),
            "news_signals": _news_signals_payload(
                db,
                limit=8,
                min_score=0.0,
                market_scope=market_scope,
            ),
        },
    )


# --- /markets list ----------------------------------------------------------


_SORT_OPTIONS = {
    "trades_desc": ("trade_count", "desc"),
    "trades_asc": ("trade_count", "asc"),
    "priority": ("priority", "desc"),  # prior rank, then trade count
    # Evidence-first: no stored anomalies => flat minimal score, so
    # high-prior markets with *no* flags sink with other quiet markets; tie
    # on `updated_at` (not on prior) within that tier.
    "surveillance_urgency": ("surveillance_urgency", "desc"),
    "top_trade_flag": ("top_trade_flag_score", "desc"),
    "recent": ("created_at", "desc"),
    "title": ("title", "asc"),
    "anomalies": ("anomaly_count", "desc"),
}


@router.get("/markets")
def list_markets(
    q: str | None = Query(
        default=None, description="Search title / subtitle / market_id"
    ),
    category: str | None = Query(default=None),
    prior: str | None = Query(default=None),
    confidence: str | None = Query(default=None),
    status: str | None = Query(default=None),
    market_scope: str = Query(default="active", description="active | historical | all"),
    include_unhydrated: bool = Query(
        default=False,
        description="Include lazy WS-created rows whose exchange metadata/title has not been hydrated yet.",
    ),
    sort: str = Query(
        default="surveillance_urgency",
        description=(
            "trades_desc | trades_asc | priority | surveillance_urgency | "
            "top_trade_flag | recent | title | anomalies — "
            "`surveillance_urgency`: zero `anomaly_count` → flat floor; else "
            "`(8+0.4*(prior_rank+1))*ln(1+count)` so evidence count dominates watch priority."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    """Paginated, filterable, sortable market list.

    Trade and anomaly counts are computed via correlated subqueries
    rather than `JOIN ... GROUP BY` so the same query plan works
    whether the user filters down to 5 rows or pages through all 24k.
    Both subqueries hit indexed FK columns and return at most one row
    per market, so they're cheap.
    """
    cache_key = (
        "markets:"
        f"q={q or ''}:category={category or ''}:prior={prior or ''}:"
        f"confidence={confidence or ''}:status={status or ''}:"
        f"market_scope={market_scope}:"
        f"include_unhydrated={int(include_unhydrated)}:sort={sort}:"
        f"limit={limit}:offset={offset}"
    )
    return _cached_dashboard_payload(
        cache_key,
        lambda: _list_markets_uncached(
            q=q,
            category=category,
            prior=prior,
            confidence=confidence,
            status=status,
            market_scope=market_scope,
            include_unhydrated=include_unhydrated,
            sort=sort,
            limit=limit,
            offset=offset,
            db=db,
        ),
    )


def _list_markets_uncached(
    *,
    q: str | None,
    category: str | None,
    prior: str | None,
    confidence: str | None,
    status: str | None,
    market_scope: str,
    include_unhydrated: bool,
    sort: str,
    limit: int,
    offset: int,
    db: Session,
) -> dict:
    filters = []
    if not include_unhydrated:
        filters.extend(_hydrated_market_filters())
    filters.extend(_market_scope_filters(market_scope))
    if q:
        like = f"%{q}%"
        filters.append(
            or_(
                Market.title.ilike(like),
                Market.subtitle.ilike(like),
                Market.market_id.ilike(like),
            )
        )
    # Breakdown charts use the string "unclassified" for NULL
    # `coalesce(Market.category, "unclassified")` — the list filter must
    # follow the same rule or clicking a bar yields empty results.
    if category:
        if category == "unclassified":
            filters.append(Market.category.is_(None))
        else:
            filters.append(Market.category == category)
    if prior:
        if prior == "unclassified":
            filters.append(Market.manipulability_prior.is_(None))
        else:
            filters.append(Market.manipulability_prior == prior)
    if confidence:
        filters.append(Market.classifier_confidence == confidence)
    if status:
        filters.append(Market.status == status)

    total_filters = [] if include_unhydrated else _hydrated_market_filters()
    total_filters.extend(_market_scope_filters(market_scope))
    total = db.query(func.count(Market.id)).filter(*total_filters).scalar() or 0
    filtered = (
        db.query(func.count(Market.id)).filter(and_(*filters)).scalar()
        if filters
        else total
    )

    if _market_metrics_available(db):
        return _list_markets_from_metric_projection(
            db=db,
            filters=filters,
            total=int(total),
            filtered=int(filtered),
            sort=sort,
            limit=limit,
            offset=offset,
        )

    candidate_sq = select(Market.id).where(and_(*filters)).subquery()
    trade_count_sq = (
        select(Trade.market_pk, func.count(Trade.id).label("c"))
        .join(candidate_sq, candidate_sq.c.id == Trade.market_pk)
        .group_by(Trade.market_pk)
        .subquery()
    )
    anomaly_count_sq = (
        select(Anomaly.market_pk, func.count(Anomaly.id).label("c"))
        .join(candidate_sq, candidate_sq.c.id == Anomaly.market_pk)
        .group_by(Anomaly.market_pk)
        .subquery()
    )
    top_trade_flag_sq = (
        select(TradeFlag.market_pk, func.max(TradeFlag.score).label("top_score"))
        .join(candidate_sq, candidate_sq.c.id == TradeFlag.market_pk)
        .group_by(TradeFlag.market_pk)
        .subquery()
    )
    base = (
        db.query(
            Market,
            func.coalesce(trade_count_sq.c.c, 0).label("trade_count"),
            func.coalesce(anomaly_count_sq.c.c, 0).label("anomaly_count"),
            func.coalesce(top_trade_flag_sq.c.top_score, 0.0).label(
                "top_trade_flag_score"
            ),
        )
        .join(candidate_sq, candidate_sq.c.id == Market.id)
        .outerjoin(trade_count_sq, trade_count_sq.c.market_pk == Market.id)
        .outerjoin(anomaly_count_sq, anomaly_count_sq.c.market_pk == Market.id)
        .outerjoin(top_trade_flag_sq, top_trade_flag_sq.c.market_pk == Market.id)
    )

    sort_key, sort_dir = _SORT_OPTIONS.get(sort, _SORT_OPTIONS["surveillance_urgency"])
    if sort_key == "trade_count":
        # NULLs (markets with no trades yet) become 0 so they sort last
        # in DESC and first in ASC. The COALESCE must wrap the column
        # *before* applying the sort direction — otherwise SQLAlchemy
        # emits `COALESCE(col DESC, 0)` which Postgres rejects.
        coalesced = func.coalesce(trade_count_sq.c.c, 0)
        base = base.order_by(coalesced.asc() if sort_dir == "asc" else coalesced.desc())
    elif sort_key == "anomaly_count":
        base = base.order_by(func.coalesce(anomaly_count_sq.c.c, 0).desc())
    elif sort_key == "top_trade_flag_score":
        base = base.order_by(
            func.coalesce(top_trade_flag_sq.c.top_score, 0.0).desc(),
            Market.updated_at.desc(),
        )
    elif sort_key == "title":
        base = base.order_by(Market.title.asc())
    elif sort_key == "created_at":
        base = base.order_by(Market.created_at.desc())
    elif sort_key == "surveillance_urgency":
        # Evidence first: for ac0>0, ln(1+ac) dominates. Prior only scales a small
        # bonus so a lower-priority market with many materialized alerts can outrank
        # a high-priority one with a single borderline row.
        ac0 = func.coalesce(anomaly_count_sq.c.c, 0)
        pr0 = func.greatest(_prior_rank(), 0)
        ln1 = func.ln(1.0 * ac0 + 1.0)
        urgency = case(
            (ac0 == 0, 1.0e-6),
            else_=(8.0 + 0.4 * (pr0 + 1.0)) * ln1,
        )
        base = base.order_by(urgency.desc(), Market.updated_at.desc())
    else:
        # `priority` — descending by prior rank, then by trade count as
        # tiebreaker. Markets with prior=NULL sort last.
        base = base.order_by(
            _prior_rank().desc(),
            func.coalesce(trade_count_sq.c.c, 0).desc(),
        )

    rows = base.offset(offset).limit(limit).all()
    latest_by_pk = _latest_snapshot_values_for_market_pks(
        db, [int(r[0].id) for r in rows]
    )

    event_ids = [r[0].event_id for r in rows if r[0].event_id]
    event_counts = {}
    if event_ids:
        event_counts = {
            event_id: int(count)
            for event_id, count in (
                db.query(Market.event_id, func.count(Market.id))
                .filter(Market.event_id.in_(event_ids))
                .filter(*_hydrated_market_filters())
                .group_by(Market.event_id)
                .all()
            )
        }

    items = [
        _serialize_market_row(
            r[0],
            trade_count=r.trade_count,
            anomaly_count=r.anomaly_count,
            last_price=latest_by_pk.get(r[0].id, {}).get("last_price"),
            volume_24h=latest_by_pk.get(r[0].id, {}).get("volume_24h"),
            top_trade_flag_score=r.top_trade_flag_score,
            event_market_count=event_counts.get(r[0].event_id)
            if r[0].event_id
            else None,
        )
        for r in rows
    ]

    return {
        "total": int(total),
        "filtered": int(filtered),
        "limit": limit,
        "offset": offset,
        "markets": items,
    }


def _list_markets_from_metric_projection(
    *,
    db: Session,
    filters: list,
    total: int,
    filtered: int,
    sort: str,
    limit: int,
    offset: int,
) -> dict:
    """Projection-first market list.

    `market_metrics` is the compact read model maintained during ingest and by
    maintenance/backfill jobs. Falling back to raw subqueries remains above for
    fresh databases with no projection rows yet.
    """

    trade_count_col = func.coalesce(MarketMetric.trade_count, 0)
    anomaly_count_col = func.coalesce(MarketMetric.anomaly_count, 0)
    urgency_col = func.coalesce(MarketMetric.urgency_score, 0.0)
    evidence_col = func.coalesce(MarketMetric.evidence_score, 0.0)
    candidate_sq = select(Market.id).where(and_(*filters)).subquery()
    top_trade_flag_sq = (
        select(TradeFlag.market_pk, func.max(TradeFlag.score).label("top_score"))
        .join(candidate_sq, candidate_sq.c.id == TradeFlag.market_pk)
        .group_by(TradeFlag.market_pk)
        .subquery()
    )
    base = (
        db.query(
            Market,
            trade_count_col.label("trade_count"),
            anomaly_count_col.label("anomaly_count"),
            urgency_col.label("metric_urgency_score"),
            evidence_col.label("metric_evidence_score"),
            func.coalesce(top_trade_flag_sq.c.top_score, 0.0).label(
                "top_trade_flag_score"
            ),
            MarketMetric.last_price_cents,
            MarketMetric.volume_24h_contracts,
            MarketMetric.storage_tier,
            MarketMetric.retention_score,
        )
        .join(candidate_sq, candidate_sq.c.id == Market.id)
        .outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
        .outerjoin(top_trade_flag_sq, top_trade_flag_sq.c.market_pk == Market.id)
    )

    sort_key, sort_dir = _SORT_OPTIONS.get(sort, _SORT_OPTIONS["surveillance_urgency"])
    if sort_key == "trade_count":
        base = base.order_by(
            trade_count_col.asc() if sort_dir == "asc" else trade_count_col.desc()
        )
    elif sort_key == "anomaly_count":
        base = base.order_by(anomaly_count_col.desc())
    elif sort_key == "top_trade_flag_score":
        base = base.order_by(
            func.coalesce(top_trade_flag_sq.c.top_score, 0.0).desc(),
            Market.updated_at.desc(),
        )
    elif sort_key == "title":
        base = base.order_by(Market.title.asc())
    elif sort_key == "created_at":
        base = base.order_by(Market.created_at.desc())
    elif sort_key == "surveillance_urgency":
        base = base.order_by(urgency_col.desc(), Market.updated_at.desc())
    else:
        base = base.order_by(_prior_rank().desc(), trade_count_col.desc())

    rows = base.offset(offset).limit(limit).all()
    pks = [int(row[0].id) for row in rows]
    latest_fallback = _latest_snapshot_values_for_market_pks(db, pks)

    event_ids = [row[0].event_id for row in rows if row[0].event_id]
    event_counts = {}
    if event_ids:
        event_counts = {
            event_id: int(count)
            for event_id, count in (
                db.query(Market.event_id, func.count(Market.id))
                .filter(Market.event_id.in_(event_ids))
                .filter(*_hydrated_market_filters())
                .group_by(Market.event_id)
                .all()
            )
        }

    items = []
    for row in rows:
        market = row[0]
        metric_last_price = (
            float(row.last_price_cents) / 100.0
            if row.last_price_cents is not None
            else None
        )
        metric_volume = (
            float(row.volume_24h_contracts)
            if row.volume_24h_contracts is not None
            else None
        )
        fallback = latest_fallback.get(market.id, {})
        items.append(
            _serialize_market_row(
                market,
                trade_count=int(row.trade_count or 0),
                anomaly_count=int(row.anomaly_count or 0),
                last_price=metric_last_price
                if metric_last_price is not None
                else fallback.get("last_price"),
                volume_24h=metric_volume
                if metric_volume is not None
                else fallback.get("volume_24h"),
                event_market_count=event_counts.get(market.event_id)
                if market.event_id
                else None,
                evidence_score=float(row.metric_evidence_score or 0.0),
                urgency_score=float(row.metric_urgency_score or 0.0),
                top_trade_flag_score=float(row.top_trade_flag_score or 0.0),
                storage_tier=row.storage_tier,
                retention_score=int(row.retention_score or 0)
                if row.retention_score is not None
                else None,
            )
        )

    return {
        "total": total,
        "filtered": filtered,
        "limit": limit,
        "offset": offset,
        "markets": items,
    }


# --- /events/{event_id} (Kalshi `event_ticker` = `markets.event_id`) -------


def _trade_counts_by_market(db: Session, pks: list[int]) -> dict[int, int]:
    if not pks:
        return {}
    rows = (
        db.query(Trade.market_pk, func.count(Trade.id).label("c"))
        .filter(Trade.market_pk.in_(pks))
        .group_by(Trade.market_pk)
        .all()
    )
    return {int(mpk): int(c) for mpk, c in rows}


def _anomaly_counts_by_market(db: Session, pks: list[int]) -> dict[int, int]:
    if not pks:
        return {}
    rows = (
        db.query(Anomaly.market_pk, func.count(Anomaly.id).label("c"))
        .filter(Anomaly.market_pk.in_(pks))
        .group_by(Anomaly.market_pk)
        .all()
    )
    return {int(mpk): int(c) for mpk, c in rows}


@router.get("/events/{event_id}")
def get_event_group(event_id: str, db: Session = Depends(get_db)) -> dict:
    """All `markets` rows sharing Kalshi’s ``event_ticker`` (e.g. date slices for one question).

    Each contract (``market_id``) stays separate; this endpoint only groups
    the UI and rolled-up read models — trades and book data remain per-ticker.
    """
    eid = (event_id or "").strip()
    if not eid:
        raise HTTPException(status_code=400, detail="event_id is required")

    markets_list = (
        db.query(Market)
        .filter(Market.event_id == eid)
        .order_by(Market.close_time.asc().nullsfirst(), Market.market_id.asc())
        .all()
    )
    if not markets_list:
        raise HTTPException(
            status_code=404,
            detail="No markets for this event_id. Check Kalshi `event_ticker` or hydrate unknown markets so `event_id` is filled.",
        )

    pks = [m.id for m in markets_list]
    tcount = _trade_counts_by_market(db, pks)
    acount = _anomaly_counts_by_market(db, pks)
    rmap = _reason_codes_for_market_pks(db, pks)

    market_payloads: list[dict] = []
    for m in markets_list:
        latest_snap = (
            db.query(MarketSnapshot)
            .filter(MarketSnapshot.market_pk == m.id)
            .order_by(MarketSnapshot.ts.desc(), MarketSnapshot.id.desc())
            .first()
        )
        last_price = (
            float(latest_snap.last_price_dollars)
            if latest_snap and latest_snap.last_price_dollars is not None
            else None
        )
        volume_24h = (
            float(latest_snap.volume_24h_fp)
            if latest_snap and latest_snap.volume_24h_fp is not None
            else None
        )
        market_payloads.append(
            _serialize_market_row(
                m,
                trade_count=tcount.get(m.id, 0),
                anomaly_count=acount.get(m.id, 0),
                last_price=last_price,
                volume_24h=volume_24h,
                reason_codes=rmap.get(m.id, []),
            )
        )

    return {
        "event_id": eid,
        "title": markets_list[0].title,
        "market_count": len(market_payloads),
        "markets": market_payloads,
    }


# --- /markets/{market_id} detail and series ---------------------------------


def _get_market_or_404(db: Session, market_id: str) -> Market:
    market = db.query(Market).filter(Market.market_id == market_id).one_or_none()
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")
    return market


@router.get("/markets/{market_id}")
def get_market_detail(market_id: str, db: Session = Depends(get_db)) -> dict:
    """Detail bundle for the market drill-down page.

    Aggregates everything the detail page needs in one round trip:
    market metadata, classifier verdict, latest snapshot prices, and
    summary stats (trade window, price extrema, volume).
    """
    market = _get_market_or_404(db, market_id)

    latest_snap = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.market_pk == market.id)
        .order_by(MarketSnapshot.ts.desc(), MarketSnapshot.id.desc())
        .first()
    )

    trade_stats = (
        db.query(
            func.count(Trade.id).label("c"),
            func.min(Trade.ts).label("first_ts"),
            func.max(Trade.ts).label("last_ts"),
            func.min(Trade.yes_price_dollars).label("min_yes"),
            func.max(Trade.yes_price_dollars).label("max_yes"),
            func.sum(Trade.count_fp).label("total_count"),
        )
        .filter(Trade.market_pk == market.id)
        .one()
    )

    anomaly_count = (
        db.query(func.count(Anomaly.id)).filter(Anomaly.market_pk == market.id).scalar()
        or 0
    )
    rmap = _reason_codes_for_market_pks(db, [market.id])
    reason_codes = rmap.get(market.id, [])

    return {
        **_serialize_market_row(
            market,
            trade_count=trade_stats.c or 0,
            anomaly_count=anomaly_count,
            last_price=float(latest_snap.last_price_dollars)
            if latest_snap and latest_snap.last_price_dollars is not None
            else None,
            volume_24h=float(latest_snap.volume_24h_fp)
            if latest_snap and latest_snap.volume_24h_fp is not None
            else None,
            reason_codes=reason_codes,
        ),
        "classifier_tags": market.classifier_tags or [],
        "news_search_query": market_news_search_query(market),
        "stats": {
            "trade_count": int(trade_stats.c or 0),
            "first_trade_ts": trade_stats.first_ts.isoformat()
            if trade_stats.first_ts
            else None,
            "last_trade_ts": trade_stats.last_ts.isoformat()
            if trade_stats.last_ts
            else None,
            "min_yes_price": float(trade_stats.min_yes)
            if trade_stats.min_yes is not None
            else None,
            "max_yes_price": float(trade_stats.max_yes)
            if trade_stats.max_yes is not None
            else None,
            "total_traded_size": float(trade_stats.total_count)
            if trade_stats.total_count is not None
            else None,
        },
        "latest_snapshot": (
            {
                "ts": latest_snap.ts.isoformat() if latest_snap.ts else None,
                "last_price_dollars": _probability_float(
                    latest_snap.last_price_dollars
                ),
                "yes_bid_dollars": _probability_float(latest_snap.yes_bid_dollars),
                "yes_ask_dollars": _probability_float(latest_snap.yes_ask_dollars),
                "volume_24h_fp": float(latest_snap.volume_24h_fp)
                if latest_snap.volume_24h_fp is not None
                else None,
                "open_interest_fp": float(latest_snap.open_interest_fp)
                if latest_snap.open_interest_fp is not None
                else None,
                "liquidity_dollars": float(latest_snap.liquidity_dollars)
                if latest_snap.liquidity_dollars is not None
                else None,
            }
            if latest_snap
            else None
        ),
    }


@router.get("/markets/{market_id}/series")
def get_market_series(
    market_id: str,
    limit: int = Query(default=2000, ge=10, le=10000),
    db: Session = Depends(get_db),
) -> dict:
    """Trade time-series + snapshot time-series for the chart.

    Returns most-recent `limit` trades (default 2000) in *ascending*
    timestamp order so `lightweight-charts` can ingest them as a series
    directly. Snapshots are returned alongside for top-of-book context.
    """
    market = _get_market_or_404(db, market_id)

    trade_rows = (
        db.query(Trade)
        .filter(Trade.market_pk == market.id)
        .order_by(Trade.ts.desc(), Trade.id.desc())
        .limit(limit)
        .all()
    )
    trade_rows = list(reversed(trade_rows))

    snap_rows = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.market_pk == market.id)
        .order_by(MarketSnapshot.ts.desc(), MarketSnapshot.id.desc())
        .limit(limit)
        .all()
    )
    snap_rows = list(reversed(snap_rows))

    trade_payloads = [
        {
            "ts": t.ts.isoformat() if t.ts else None,
            "yes_price": _probability_float(t.yes_price_dollars),
            "no_price": _probability_float(t.no_price_dollars),
            "count": float(t.count_fp) if t.count_fp is not None else None,
            "trade_dollar_amount": _trade_notional_dollars(
                yes_price=t.yes_price_dollars,
                no_price=t.no_price_dollars,
                count=t.count_fp,
                taker_side=t.taker_side,
            ),
            "taker_side": t.taker_side,
        }
        for t in trade_rows
    ]
    snapshot_payloads = [_snapshot_payload(s) for s in snap_rows]
    explanations = explain_trades_against_window(trade_payloads, window=50)
    peer_rows = _peer_baseline_rows_for_market(db, market)
    peer_baselines = build_peer_baselines(peer_rows, min_points=12)
    peer_key = (
        str(market.category or "unclassified"),
        str(market.subcategory or "*"),
    )
    first_trade_ts = trade_rows[0].ts if trade_rows else None
    last_trade_ts = trade_rows[-1].ts if trade_rows else None
    contextual = explain_trades_with_context(
        trade_payloads,
        market=_market_context(market),
        snapshots=snapshot_payloads,
        local_explanations=explanations,
        peer_baseline=peer_baselines.get(peer_key)
        or peer_baselines.get((peer_key[0], "*")),
        news_events=_news_context_for_market(db, market),
        sibling_snapshots=_sibling_snapshot_context(
            db,
            market,
            start=first_trade_ts,
            end=last_trade_ts,
        ),
    )
    for i, explanation in enumerate(explanations):
        context = contextual[i] if i < len(contextual) else None
        local_score = float(explanation["score"]) if explanation is not None else 0.0
        context_score = float(context["score"]) if context is not None else 0.0
        combined_score = max(local_score, context_score)
        if explanation is not None:
            trade_payloads[i]["local_suspicion"] = explanation["score"]
        if context is not None:
            trade_payloads[i]["context_score"] = context["score"]
            trade_payloads[i]["context_reasons"] = context["reasons"]
            trade_payloads[i]["context_components"] = context["components"]
            trade_payloads[i]["context_features"] = context["features"]
        if explanation is not None or context is not None:
            trade_payloads[i]["suspicion"] = combined_score
            trade_payloads[i]["suspicion_reasons"] = sorted(
                set(
                    (explanation or {}).get("reasons", [])
                    + (context or {}).get("reasons", [])
                )
            )
            trade_payloads[i]["suspicion_features"] = {
                **((explanation or {}).get("features", {})),
                "context": (context or {}).get("features", {}),
                "components": (context or {}).get("components", {}),
            }

    burst = analyze_tape_bursts(trade_payloads, window_sec=30.0)
    for i, c in enumerate(burst["per_trade_cluster_0_10"]):
        trade_payloads[i]["cluster_0_10"] = c

    return {
        "market_id": market.market_id,
        "tape_cluster": {
            "burst_score_0_10": burst["burst_score_0_10"],
            "largest_window_count": burst["largest_window_count"],
            "window_sec": burst["window_sec"],
            "dominant_side": burst["dominant_side"],
        },
        "trades": trade_payloads,
        "snapshots": snapshot_payloads,
    }


@router.get("/markets/{market_id}/anomalies")
def get_market_anomalies(
    market_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """Anomalies stored against this market, newest first."""
    market = _get_market_or_404(db, market_id)

    rows = (
        db.query(Anomaly)
        .filter(Anomaly.market_pk == market.id)
        .order_by(Anomaly.created_at.desc(), Anomaly.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "count": len(rows),
        "anomalies": [
            {
                "id": a.id,
                "score": float(a.score),
                "severity": a.severity,
                "reasons": a.reasons,
                "signals": a.signals,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in rows
        ],
    }


# --- /anomalies and /top-markets (for overview page) ------------------------


@router.get("/anomalies")
def list_recent_anomalies(
    limit: int = Query(default=20, ge=1, le=100),
    severity: str | None = Query(default=None),
    market_scope: str = Query(default="active", description="active | historical | all"),
    db: Session = Depends(get_db),
) -> dict:
    """Recent anomalies across all markets, with the parent market's
    classifier metadata so the frontend can render a category badge
    without a second round trip.
    """
    cache_key = f"recent_anomalies:{limit}:{severity or ''}:{market_scope}"
    return _cached_dashboard_payload(
        cache_key,
        lambda: _recent_anomalies_payload(
            db, limit, severity, market_scope=market_scope
        ),
    )


@router.get("/top-markets")
def get_top_markets(
    limit: int = Query(default=15, ge=1, le=50),
    market_scope: str = Query(default="active", description="active | historical | all"),
    db: Session = Depends(get_db),
) -> dict:
    """Top markets by trade count, for the overview leaderboard."""
    return _cached_dashboard_payload(
        f"top_markets:{limit}:{market_scope}",
        lambda: _top_markets_payload(db, limit, market_scope=market_scope),
    )


@router.get("/suspicious-trades")
def get_suspicious_trades(
    limit: int = Query(default=25, ge=1, le=100),
    market_scope: str = Query(default="active", description="active | historical | all"),
    db: Session = Depends(get_db),
) -> dict:
    return _cached_dashboard_payload(
        f"suspicious_trades:{limit}:{market_scope}",
        lambda: _suspicious_trades_payload(
            db, limit=limit, market_scope=market_scope
        ),
    )


@router.get("/news-signals")
def get_news_signals(
    limit: int = Query(default=25, ge=1, le=100),
    min_score: float = Query(default=4.0, ge=0.0, le=10.0),
    status: str | None = Query(default=None),
    include_ambiguous: bool = Query(default=False),
    market_scope: str = Query(default="active", description="active | historical | all"),
    db: Session = Depends(get_db),
) -> dict:
    return _cached_dashboard_payload(
        f"news_signals:{limit}:{min_score}:{status or ''}:"
        f"{int(include_ambiguous)}:{market_scope}",
        lambda: _news_signals_payload(
            db,
            limit=limit,
            min_score=min_score,
            status=status,
            include_ambiguous=include_ambiguous,
            market_scope=market_scope,
        ),
    )


# --- /news (GDELT 2.0 DOC API with graceful degradation) --------------------


_GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def _stored_news_for_market(
    db: Session,
    market: Market,
    *,
    limit: int,
    align: str,
) -> list[dict]:
    q = (
        db.query(NewsEvent, NewsArticle)
        .join(NewsArticle, NewsArticle.id == NewsEvent.article_id)
        .filter(NewsEvent.market_pk == market.id)
        .filter(NewsEvent.relevance_score >= 0.35)
    )
    if align == "activity":
        q = q.order_by(
            NewsEvent.pre_news_trade_score.desc(),
            NewsArticle.first_seen_at.desc(),
            NewsEvent.id.desc(),
        )
    else:
        q = q.order_by(NewsArticle.first_seen_at.desc(), NewsEvent.id.desc())
    rows = q.limit(limit * 3).all()
    out: list[dict] = []
    for event, article in rows:
        if _is_weak_factor_only_news_event(event):
            continue
        current_link = _current_news_link_for_market(article, market)
        if current_link is None:
            continue
        current_relevance, current_components = current_link
        out.append(
            _linked_news_article_payload(
                event,
                article,
                components_override=current_components,
                relevance_score_override=current_relevance,
            )
        )
        if len(out) >= limit:
            break
    return out


@router.get("/markets/{market_id}/news")
async def get_market_news(
    market_id: str,
    limit: int = Query(default=10, ge=1, le=30),
    align: str = Query(
        default="default",
        description="`default` = 30d before close. `activity` = ~7d before latest trade/flag, for comparing headlines to the tape.",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """Correlated news for this market via GDELT 2.0 DOC API.

    GDELT is a free, no-key news index covering hundreds of thousands
    of sources globally. We hit it best-effort with a 5-second timeout.
    On any failure (network, parse, GDELT service down) we return an
    empty article list with `provider="unavailable"` so the UI can show
    a graceful empty state instead of an error toast.
    """
    if align not in ("default", "activity"):
        raise HTTPException(
            status_code=400, detail="align must be 'default' or 'activity'"
        )
    market = _get_market_or_404(db, market_id)
    last_trade = (
        db.query(func.max(Trade.ts)).filter(Trade.market_pk == market.id).scalar()
    )
    last_flag = (
        db.query(func.max(Anomaly.created_at))
        .filter(Anomaly.market_pk == market.id)
        .scalar()
    )
    start, end, anchors = choose_news_window(
        market,
        last_trade=last_trade,
        last_flag=last_flag,
        align=align,
    )
    query = build_gdelt_query(market)
    payload: dict = {
        "market_id": market.market_id,
        "query": query,
        "since": start.isoformat(),
        "until": end.isoformat(),
        "anchors": anchors,
        "provider": "gdelt",
        "articles": [],
    }

    if not query.strip():
        return {**payload, "provider": "unavailable", "error": "empty query"}

    stored_articles = _stored_news_for_market(
        db,
        market,
        limit=limit,
        align=align,
    )
    if stored_articles:
        return {
            **payload,
            "provider": "stored",
            "articles": stored_articles,
            "stored_event_count": len(stored_articles),
        }

    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(limit),
        "sort": "DateDesc",
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(_GDELT_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        logger.info("gdelt unavailable for %s: %s", market_id, e)
        return {**payload, "provider": "unavailable", "error": str(e)}
    except ValueError as e:
        # GDELT occasionally returns HTML error pages with 200 status.
        logger.info("gdelt non-json response for %s: %s", market_id, e)
        return {**payload, "provider": "unavailable", "error": "non-json response"}

    raw_articles = data.get("articles") or []
    articles = []
    for a in raw_articles:
        seendate = a.get("seendate") or ""
        try:
            published = datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
            published_iso = published.isoformat()
        except ValueError:
            published_iso = None
        articles.append(
            {
                "title": a.get("title"),
                "url": a.get("url"),
                "source": a.get("domain"),
                "language": a.get("language"),
                "published_at": published_iso,
                "tone": a.get("tone"),
            }
        )

    payload["articles"] = articles
    return payload
