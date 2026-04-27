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
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.core.config import settings
from app.db.models import (
    Anomaly,
    Market,
    MarketSnapshot,
    NewsArticle,
    NewsEvent,
    Trade,
    TradeFlag,
)
from app.services.news_gdelt import build_gdelt_query, choose_news_window
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
    reason_codes: list[str] | None = None,
    event_market_count: int | None = None,
) -> dict:
    prk = prior_rank(market.manipulability_prior)
    ac = int(anomaly_count or 0)
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
        "trade_count": int(trade_count or 0),
        "anomaly_count": ac,
        "last_price": float(last_price) if last_price is not None else None,
        "volume_24h": float(volume_24h) if volume_24h is not None else None,
        "market_priority": market_priority_value(market.manipulability_prior),
        "evidence_score": evidence_score_0_100(anomaly_count=ac),
        "urgency_score": urgency_score_0_100(prior_rank=prk, anomaly_count=ac),
        "reasons": list(reason_codes or []),
        "event_market_count": event_market_count,
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


def _snapshot_payload(snapshot: MarketSnapshot) -> dict:
    return {
        "ts": snapshot.ts.isoformat() if snapshot.ts else None,
        "market_pk": snapshot.market_pk,
        "yes_bid": float(snapshot.yes_bid_dollars)
        if snapshot.yes_bid_dollars is not None
        else None,
        "yes_ask": float(snapshot.yes_ask_dollars)
        if snapshot.yes_ask_dollars is not None
        else None,
        "last_price": float(snapshot.last_price_dollars)
        if snapshot.last_price_dollars is not None
        else None,
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
            "yes_price": float(t.yes_price_dollars)
            if t.yes_price_dollars is not None
            else None,
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


def _hydrated_market_filters() -> list:
    return [
        Market.status.notin_(("unknown", "out_of_scope")),
        Market.title != Market.market_id,
    ]


def _stats_payload(db: Session) -> dict:
    """Coarse system-wide counts. Used by `GET /stats` and `GET /overview`."""
    hydrated = _hydrated_market_filters()
    markets = db.query(func.count(Market.id)).filter(*hydrated).scalar() or 0
    markets_unknown = (
        db.query(func.count(Market.id)).filter(Market.status == "unknown").scalar() or 0
    )
    markets_high_prior = (
        db.query(func.count(Market.id))
        .filter(*hydrated)
        .filter(Market.manipulability_prior.in_(("high", "medium_high")))
        .scalar()
        or 0
    )
    trades = _estimated_table_count(db, "trades")
    snapshots = _estimated_table_count(db, "market_snapshots")
    book_events = _estimated_table_count(db, "book_events")
    anomalies = _estimated_table_count(db, "anomalies")
    anomalies_high = (
        db.query(func.count(Anomaly.id)).filter(Anomaly.severity == "high").scalar()
        or 0
    )
    markets_with_flags = min(markets, anomalies)

    return {
        "markets": markets,
        "markets_status_unknown": markets_unknown,
        "markets_high_prior": markets_high_prior,
        "markets_with_flags": markets_with_flags,
        "trades": trades,
        "snapshots": snapshots,
        "book_events": book_events,
        "anomalies": anomalies,
        "anomalies_high_severity": anomalies_high,
    }


def _breakdown_payload(db: Session) -> dict:
    """Classification pivots for the overview page."""

    hydrated = _hydrated_market_filters()

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
        .limit(40)
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
def get_stats(db: Session = Depends(get_db)) -> dict:
    """Coarse system-wide counts. Drives the overview header."""
    return _cached_dashboard_payload("stats", lambda: _stats_payload(db))


@router.get("/breakdown")
def get_breakdown(db: Session = Depends(get_db)) -> dict:
    """Classification pivots for the overview page.

    Returns counts grouped by each of the four classifier dimensions
    (`category`, `manipulability_prior`, `classifier_confidence`,
    `classifier_layer`) plus a category × prior cross-tab for the
    "where's the high-prior cluster?" question. NULLs are bucketed as
    `"unclassified"` so the frontend can render them without special
    casing.
    """
    return _cached_dashboard_payload("breakdown", lambda: _breakdown_payload(db))


def _top_markets_payload(db: Session, limit: int) -> dict:
    recent_trades = (
        select(Trade.market_pk, Trade.ts, Trade.id)
        .join(Market, Market.id == Trade.market_pk)
        .where(*_hydrated_market_filters())
        .order_by(Trade.ts.desc(), Trade.id.desc())
        .limit(_TOP_MARKETS_RECENT_TRADE_SAMPLE)
        .subquery()
    )
    trade_rows = (
        db.query(recent_trades.c.market_pk, func.count().label("trade_count"))
        .group_by(recent_trades.c.market_pk)
        .order_by(func.count().desc())
        .limit(limit)
        .all()
    )

    pks = [int(r.market_pk) for r in trade_rows]
    if not pks:
        return {"count": 0, "markets": []}

    trade_counts = {int(r.market_pk): int(r.trade_count or 0) for r in trade_rows}
    markets_by_pk = {m.id: m for m in db.query(Market).filter(Market.id.in_(pks)).all()}

    rows = [markets_by_pk[pk] for pk in pks if pk in markets_by_pk]
    return {
        "count": len(rows),
        "markets": [
            _serialize_market_row(
                market,
                trade_count=trade_counts.get(market.id, 0),
            )
            for market in rows
        ],
    }


def _recent_anomalies_payload(db: Session, limit: int, severity: str | None) -> dict:
    base = db.query(Anomaly, Market).join(Market, Market.id == Anomaly.market_pk)
    base = base.filter(*_hydrated_market_filters())
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


def _suspicious_trades_payload(
    db: Session,
    *,
    limit: int,
    sample: int = _SUSPICIOUS_TRADE_SAMPLE,
) -> dict:
    persisted = (
        db.query(TradeFlag, Trade, Market)
        .join(Trade, Trade.id == TradeFlag.trade_pk)
        .join(Market, Market.id == TradeFlag.market_pk)
        .filter(*_hydrated_market_filters())
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
        .filter(*_hydrated_market_filters())
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
    severity: str | None = Query(
        default=None,
        description="If set, filter recent-anomalies in this bundle the same as `GET /anomalies`.",
    ),
    db: Session = Depends(get_db),
) -> dict:
    """One round-trip for the home page: stats, charts, top markets, recent flags.

    Cuts TTFB vs four separate fetches; still runs the same SQL as those routes.
    """
    cache_key = f"overview:{top}:{anomalies}:{severity or ''}"
    return _cached_dashboard_payload(
        cache_key,
        lambda: {
            "stats": _stats_payload(db),
            "breakdown": _breakdown_payload(db),
            "top_markets": _top_markets_payload(db, top),
            "recent_anomalies": _recent_anomalies_payload(db, anomalies, severity),
            "suspicious_trades": _suspicious_trades_payload(db, limit=12),
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
    include_unhydrated: bool = Query(
        default=False,
        description="Include lazy WS-created rows whose exchange metadata/title has not been hydrated yet.",
    ),
    sort: str = Query(
        default="surveillance_urgency",
        description=(
            "trades_desc | trades_asc | priority | surveillance_urgency | "
            "recent | title | anomalies — "
            "`surveillance_urgency`: zero `anomaly_count` → flat floor; else "
            "`(8+0.4*(prior_rank+1))*ln(1+count)` so evidence count dominates triage."
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
    include_unhydrated: bool,
    sort: str,
    limit: int,
    offset: int,
    db: Session,
) -> dict:
    filters = []
    if not include_unhydrated:
        filters.extend(_hydrated_market_filters())
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
    total = db.query(func.count(Market.id)).filter(*total_filters).scalar() or 0
    filtered = (
        db.query(func.count(Market.id)).filter(and_(*filters)).scalar()
        if filters
        else total
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
    base = (
        db.query(
            Market,
            func.coalesce(trade_count_sq.c.c, 0).label("trade_count"),
            func.coalesce(anomaly_count_sq.c.c, 0).label("anomaly_count"),
        )
        .join(candidate_sq, candidate_sq.c.id == Market.id)
        .outerjoin(trade_count_sq, trade_count_sq.c.market_pk == Market.id)
        .outerjoin(anomaly_count_sq, anomaly_count_sq.c.market_pk == Market.id)
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
    elif sort_key == "title":
        base = base.order_by(Market.title.asc())
    elif sort_key == "created_at":
        base = base.order_by(Market.created_at.desc())
    elif sort_key == "surveillance_urgency":
        # Evidence first: for ac0>0, ln(1+ac) dominates. Prior only scales a small
        # bonus so a lower-triage market with many materialized flags can outrank
        # a high-triage one with a single borderline row.
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
                "last_price_dollars": float(latest_snap.last_price_dollars)
                if latest_snap.last_price_dollars is not None
                else None,
                "yes_bid_dollars": float(latest_snap.yes_bid_dollars)
                if latest_snap.yes_bid_dollars is not None
                else None,
                "yes_ask_dollars": float(latest_snap.yes_ask_dollars)
                if latest_snap.yes_ask_dollars is not None
                else None,
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
            "yes_price": float(t.yes_price_dollars)
            if t.yes_price_dollars is not None
            else None,
            "no_price": float(t.no_price_dollars)
            if t.no_price_dollars is not None
            else None,
            "count": float(t.count_fp) if t.count_fp is not None else None,
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
    db: Session = Depends(get_db),
) -> dict:
    """Recent anomalies across all markets, with the parent market's
    classifier metadata so the frontend can render a category badge
    without a second round trip.
    """
    cache_key = f"recent_anomalies:{limit}:{severity or ''}"
    return _cached_dashboard_payload(
        cache_key,
        lambda: _recent_anomalies_payload(db, limit, severity),
    )


@router.get("/top-markets")
def get_top_markets(
    limit: int = Query(default=15, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    """Top markets by trade count, for the overview leaderboard."""
    return _cached_dashboard_payload(
        f"top_markets:{limit}",
        lambda: _top_markets_payload(db, limit),
    )


@router.get("/suspicious-trades")
def get_suspicious_trades(
    limit: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    return _cached_dashboard_payload(
        f"suspicious_trades:{limit}",
        lambda: _suspicious_trades_payload(db, limit=limit),
    )


# --- /news (GDELT 2.0 DOC API with graceful degradation) --------------------


_GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


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
