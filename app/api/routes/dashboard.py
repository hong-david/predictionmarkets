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
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, desc, distinct, func, or_, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.db.models import Anomaly, BookEvent, Market, MarketSnapshot, Trade
from app.services.news_gdelt import build_gdelt_query, choose_news_window
from app.services.trade_suspicion import score_trades_against_window

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
logger = logging.getLogger(__name__)


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


def _serialize_market_row(
    market: Market,
    *,
    trade_count: int = 0,
    anomaly_count: int = 0,
    last_price: float | None = None,
    volume_24h: float | None = None,
) -> dict:
    return {
        "market_id": market.market_id,
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
        "anomaly_count": int(anomaly_count or 0),
        "last_price": float(last_price) if last_price is not None else None,
        "volume_24h": float(volume_24h) if volume_24h is not None else None,
    }


# --- /stats and /breakdown --------------------------------------------------


def _stats_payload(db: Session) -> dict:
    """Coarse system-wide counts. Used by `GET /stats` and `GET /overview`."""
    markets = db.query(func.count(Market.id)).scalar() or 0
    markets_unknown = (
        db.query(func.count(Market.id)).filter(Market.status == "unknown").scalar() or 0
    )
    markets_high_prior = (
        db.query(func.count(Market.id))
        .filter(Market.manipulability_prior.in_(("high", "medium_high")))
        .scalar()
        or 0
    )
    trades = db.query(func.count(Trade.id)).scalar() or 0
    snapshots = db.query(func.count(MarketSnapshot.id)).scalar() or 0
    book_events = db.query(func.count(BookEvent.id)).scalar() or 0
    anomalies = db.query(func.count(Anomaly.id)).scalar() or 0
    anomalies_high = (
        db.query(func.count(Anomaly.id)).filter(Anomaly.severity == "high").scalar() or 0
    )
    markets_with_flags = (
        db.query(func.count(distinct(Anomaly.market_pk))).scalar() or 0
    )

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

    def _group_count(col):
        rows = (
            db.query(func.coalesce(col, "unclassified").label("k"), func.count(Market.id))
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
    return _stats_payload(db)


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
    return _breakdown_payload(db)


def _top_markets_payload(db: Session, limit: int) -> dict:
    trade_count_sq = (
        select(Trade.market_pk, func.count(Trade.id).label("c"))
        .group_by(Trade.market_pk)
        .subquery()
    )
    rows = (
        db.query(
            Market,
            func.coalesce(trade_count_sq.c.c, 0).label("trade_count"),
        )
        .outerjoin(trade_count_sq, trade_count_sq.c.market_pk == Market.id)
        .order_by(func.coalesce(trade_count_sq.c.c, 0).desc())
        .limit(limit)
        .all()
    )
    return {
        "count": len(rows),
        "markets": [
            _serialize_market_row(r[0], trade_count=r.trade_count)
            for r in rows
        ],
    }


def _recent_anomalies_payload(db: Session, limit: int, severity: str | None) -> dict:
    base = db.query(Anomaly, Market).join(Market, Market.id == Anomaly.market_pk)
    if severity:
        base = base.filter(Anomaly.severity == severity)
    rows = base.order_by(Anomaly.score.desc(), Anomaly.id.desc()).limit(limit).all()
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
    return {
        "stats": _stats_payload(db),
        "breakdown": _breakdown_payload(db),
        "top_markets": _top_markets_payload(db, top),
        "recent_anomalies": _recent_anomalies_payload(db, anomalies, severity),
    }


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
    q: str | None = Query(default=None, description="Search title / subtitle / market_id"),
    category: str | None = Query(default=None),
    prior: str | None = Query(default=None),
    confidence: str | None = Query(default=None),
    status: str | None = Query(default=None),
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
    trade_count_sq = (
        select(Trade.market_pk, func.count(Trade.id).label("c"))
        .group_by(Trade.market_pk)
        .subquery()
    )
    anomaly_count_sq = (
        select(Anomaly.market_pk, func.count(Anomaly.id).label("c"))
        .group_by(Anomaly.market_pk)
        .subquery()
    )

    base = (
        db.query(
            Market,
            func.coalesce(trade_count_sq.c.c, 0).label("trade_count"),
            func.coalesce(anomaly_count_sq.c.c, 0).label("anomaly_count"),
        )
        .outerjoin(trade_count_sq, trade_count_sq.c.market_pk == Market.id)
        .outerjoin(anomaly_count_sq, anomaly_count_sq.c.market_pk == Market.id)
    )

    filters = []
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

    if filters:
        base = base.filter(and_(*filters))

    total = db.query(func.count(Market.id)).scalar() or 0
    filtered = (
        db.query(func.count(Market.id)).filter(and_(*filters)).scalar()
        if filters
        else total
    )

    sort_key, sort_dir = _SORT_OPTIONS.get(
        sort, _SORT_OPTIONS["surveillance_urgency"]
    )
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

    items = [
        _serialize_market_row(
            r[0],
            trade_count=r.trade_count,
            anomaly_count=r.anomaly_count,
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
        db.query(func.count(Anomaly.id)).filter(Anomaly.market_pk == market.id).scalar() or 0
    )

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
        ),
        "classifier_tags": market.classifier_tags or [],
        "stats": {
            "trade_count": int(trade_stats.c or 0),
            "first_trade_ts": trade_stats.first_ts.isoformat() if trade_stats.first_ts else None,
            "last_trade_ts": trade_stats.last_ts.isoformat() if trade_stats.last_ts else None,
            "min_yes_price": float(trade_stats.min_yes) if trade_stats.min_yes is not None else None,
            "max_yes_price": float(trade_stats.max_yes) if trade_stats.max_yes is not None else None,
            "total_traded_size": float(trade_stats.total_count) if trade_stats.total_count is not None else None,
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
            "yes_price": float(t.yes_price_dollars) if t.yes_price_dollars is not None else None,
            "no_price": float(t.no_price_dollars) if t.no_price_dollars is not None else None,
            "count": float(t.count_fp) if t.count_fp is not None else None,
            "taker_side": t.taker_side,
        }
        for t in trade_rows
    ]
    sus = score_trades_against_window(trade_payloads, window=50)
    for i, s in enumerate(sus):
        if s is not None:
            trade_payloads[i]["suspicion"] = s

    return {
        "market_id": market.market_id,
        "trades": trade_payloads,
        "snapshots": [
            {
                "ts": s.ts.isoformat() if s.ts else None,
                "yes_bid": float(s.yes_bid_dollars) if s.yes_bid_dollars is not None else None,
                "yes_ask": float(s.yes_ask_dollars) if s.yes_ask_dollars is not None else None,
                "last_price": float(s.last_price_dollars) if s.last_price_dollars is not None else None,
                "volume_24h": float(s.volume_24h_fp) if s.volume_24h_fp is not None else None,
                "open_interest": float(s.open_interest_fp) if s.open_interest_fp is not None else None,
            }
            for s in snap_rows
        ],
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
    return _recent_anomalies_payload(db, limit, severity)


@router.get("/top-markets")
def get_top_markets(
    limit: int = Query(default=15, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    """Top markets by trade count, for the overview leaderboard."""
    return _top_markets_payload(db, limit)


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
