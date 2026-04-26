from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.db.models import Anomaly, Market, MarketSnapshot
from app.services.anomaly_engine import analyze_market

router = APIRouter(tags=["anomalies"])


@router.get("/markets/{market_id}/anomaly")
def get_market_anomaly(
    market_id: str,
    lookback: int = Query(default=5, ge=1, le=50),
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

    return analyze_market(market, snapshots)

@router.get("/anomalies/stored")
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
    
@router.get("/anomalies")
def list_anomalies(
    market_limit: int = Query(default=50, ge=1, le=200),
    result_limit: int = Query(default=20, ge=1, le=100),
    lookback: int = Query(default=5, ge=1, le=50),
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

        analysis = analyze_market(market, snapshots)
        results.append(analysis)

    results.sort(key=lambda x: x["score"], reverse=True)

    return {
        "market_limit_scanned": market_limit,
        "result_count": min(len(results), result_limit),
        "anomalies": results[:result_limit],
    }