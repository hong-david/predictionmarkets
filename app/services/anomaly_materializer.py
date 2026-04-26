from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Anomaly, Market, MarketSnapshot
from app.services.anomaly_engine import analyze_market
from app.services.book_activity_signals import collect_book_activity_signals

# Do not store / refresh rows for weak scores — they dominated the market-detail
# chart. ~3.0 ≈ a single "medium" rule firing with headroom, or a few stacked
# low signals. Tune alongside `anomaly_engine` thresholds.
_MIN_SCORE_TO_PERSIST = 3.0


def materialize_market_anomaly(
    db: Session,
    market: Market,
    lookback: int = 40,
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
        return {
            "created_anomalies": 0,
            "updated_anomalies": 0,
            "deleted_anomalies": 0,
        }

    book_raw = collect_book_activity_signals(db, market.id)
    analysis = analyze_market(market, snapshots, book_activity=book_raw)
    effective_latest_snapshot_id = latest_snapshot_id or snapshots[0].id
    score = float(analysis["score"])

    existing = (
        db.query(Anomaly)
        .filter(
            Anomaly.market_pk == market.id,
            Anomaly.latest_snapshot_id == effective_latest_snapshot_id,
        )
        .one_or_none()
    )

    if score < _MIN_SCORE_TO_PERSIST:
        if existing is not None:
            db.delete(existing)
            return {
                "created_anomalies": 0,
                "updated_anomalies": 0,
                "deleted_anomalies": 1,
            }
        return {
            "created_anomalies": 0,
            "updated_anomalies": 0,
            "deleted_anomalies": 0,
        }

    if existing is not None:
        existing.score = analysis["score"]
        existing.severity = analysis["severity"]
        existing.reasons = analysis["reasons"]
        existing.signals = analysis["signals"]
        return {
            "created_anomalies": 0,
            "updated_anomalies": 1,
            "deleted_anomalies": 0,
        }

    row = Anomaly(
        market_pk=market.id,
        latest_snapshot_id=effective_latest_snapshot_id,
        score=analysis["score"],
        severity=analysis["severity"],
        reasons=analysis["reasons"],
        signals=analysis["signals"],
    )
    db.add(row)
    return {
        "created_anomalies": 1,
        "updated_anomalies": 0,
        "deleted_anomalies": 0,
    }


def materialize_anomalies(
    db: Session,
    market_limit: int = 100,
    lookback: int = 40,
) -> dict[str, int]:
    created = 0
    updated = 0
    deleted = 0

    markets = db.query(Market).order_by(Market.id.desc()).limit(market_limit).all()

    for market in markets:
        result = materialize_market_anomaly(db, market, lookback=lookback)
        created += result["created_anomalies"]
        updated += result["updated_anomalies"]
        deleted += result["deleted_anomalies"]

    db.commit()

    return {
        "created_anomalies": created,
        "updated_anomalies": updated,
        "deleted_anomalies": deleted,
    }
