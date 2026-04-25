from sqlalchemy.orm import Session

from app.db.models import Anomaly, Market, MarketSnapshot
from app.services.anomaly_engine import analyze_market


def materialize_market_anomaly(
    db: Session,
    market: Market,
    lookback: int = 5,
    latest_snapshot_id: int | None = None,
) -> dict[str, int]:
    snapshots = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.market_pk == market.id)
        .order_by(MarketSnapshot.ts.desc(), MarketSnapshot.id.desc())
        .limit(lookback)
        .all()
    )

    if not snapshots:
        return {"created_anomalies": 0, "updated_anomalies": 0}

    analysis = analyze_market(market, snapshots)
    effective_latest_snapshot_id = latest_snapshot_id or snapshots[0].id

    existing = (
        db.query(Anomaly)
        .filter(
            Anomaly.market_pk == market.id,
            Anomaly.latest_snapshot_id == effective_latest_snapshot_id,
        )
        .one_or_none()
    )

    if existing:
        existing.score = analysis["score"]
        existing.severity = analysis["severity"]
        existing.reasons = analysis["reasons"]
        existing.signals = analysis["signals"]
        return {"created_anomalies": 0, "updated_anomalies": 1}

    row = Anomaly(
        market_pk=market.id,
        latest_snapshot_id=effective_latest_snapshot_id,
        score=analysis["score"],
        severity=analysis["severity"],
        reasons=analysis["reasons"],
        signals=analysis["signals"],
    )
    db.add(row)
    return {"created_anomalies": 1, "updated_anomalies": 0}


def materialize_anomalies(
    db: Session,
    market_limit: int = 100,
    lookback: int = 5,
) -> dict[str, int]:
    created = 0
    updated = 0

    markets = db.query(Market).order_by(Market.id.desc()).limit(market_limit).all()

    for market in markets:
        result = materialize_market_anomaly(db, market, lookback=lookback)
        created += result["created_anomalies"]
        updated += result["updated_anomalies"]

    db.commit()

    return {
        "created_anomalies": created,
        "updated_anomalies": updated,
    }