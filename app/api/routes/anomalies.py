from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.db.models import Anomaly, Market, MarketSnapshot
from app.services.anomaly_engine import analyze_market
from app.services.book_activity_signals import collect_book_activity_signals

router = APIRouter(tags=["anomalies (legacy on-the-fly)"], deprecated=True)


@router.get(
    "/markets/{market_id}/anomaly",
    summary="Recompute anomaly from snapshots (legacy)",
    description="Prefer `GET /api/dashboard/markets/{id}/anomalies` for materialized rule rows; "
    "this endpoint re-runs the engine for debugging.",
)
def get_market_anomaly(
    market_id: str,
    lookback: int = Query(default=40, ge=1, le=80),
    db: Session = Depends(get_db),
) -> dict:
    market = db.query(Market).filter(Market.market_id == market_id).one_or_none()
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")

    snapshots = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.market_pk == market.id)
        .order_by(MarketSnapshot.ts.desc(), MarketSnapshot.id.desc())
        .limit(lookback)
        .all()
    )

    book_raw = collect_book_activity_signals(db, market.id)
    return analyze_market(market, snapshots, book_activity=book_raw)


@router.get(
    "/anomalies/stored",
    summary="List stored anomalies (legacy)",
    description="Prefer `GET /api/dashboard/anomalies` for the same materialized list shape.",
)
def list_stored_anomalies(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    rows = (
        db.query(Anomaly, Market)
        .join(Market, Market.id == Anomaly.market_pk)
        .order_by(Anomaly.score.desc(), Anomaly.id.desc())
        .limit(limit)
        .all()
    )

    results = []
    for anomaly, market in rows:
        results.append(
            {
                "id": anomaly.id,
                "market_id": market.market_id,
                "title": market.title,
                # Multiple Kalshi markets share an event-level `title`
                # (e.g. each side / strike of an NBA spread); `subtitle`
                # is the leg-level differentiator that lets the dashboard
                # tell them apart.
                "subtitle": market.subtitle,
                "score": float(anomaly.score),
                "severity": anomaly.severity,
                "reasons": anomaly.reasons,
                "signals": anomaly.signals,
                "created_at": anomaly.created_at.isoformat() if anomaly.created_at else None,
            }
        )

    return {
        "count": len(results),
        "anomalies": results,
    }


@router.get(
    "/anomalies",
    summary="Scan + analyze markets (legacy, heavy)",
    description="Prefer the dashboard: `GET /api/dashboard/overview` and `GET /api/dashboard/anomalies` "
    "read precomputed `anomalies` without scanning large snapshot windows per request.",
)
def list_anomalies(
    market_limit: int = Query(default=50, ge=1, le=200),
    result_limit: int = Query(default=20, ge=1, le=100),
    lookback: int = Query(default=40, ge=1, le=80),
    db: Session = Depends(get_db),
) -> dict:
    markets = (
        db.query(Market)
        .order_by(Market.id.desc())
        .limit(market_limit)
        .all()
    )

    results = []

    for market in markets:
        snapshots = (
            db.query(MarketSnapshot)
            .filter(MarketSnapshot.market_pk == market.id)
            .order_by(MarketSnapshot.ts.desc(), MarketSnapshot.id.desc())
            .limit(lookback)
            .all()
        )

        if not snapshots:
            continue

        book_raw = collect_book_activity_signals(db, market.id)
        analysis = analyze_market(market, snapshots, book_activity=book_raw)
        results.append(analysis)

    results.sort(key=lambda x: x["score"], reverse=True)

    return {
        "market_limit_scanned": market_limit,
        "result_count": min(len(results), result_limit),
        "anomalies": results[:result_limit],
    }
