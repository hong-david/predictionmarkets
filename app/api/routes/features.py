from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.db.models import Market, MarketSnapshot

router = APIRouter(prefix="/markets", tags=["features"])


def decimal_to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


@router.get("/{market_id}/features")
def get_market_features(
    market_id: str,
    db: Session = Depends(get_db),
) -> dict:
    market = db.query(Market).filter(Market.market_id == market_id).one_or_none()
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")

    latest_snapshot = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.market_pk == market.id)
        .order_by(MarketSnapshot.ts.desc(), MarketSnapshot.id.desc())
        .first()
    )

    if latest_snapshot is None:
        raise HTTPException(status_code=404, detail="No snapshots found for market")

    yes_bid = latest_snapshot.yes_bid_dollars
    yes_ask = latest_snapshot.yes_ask_dollars

    spread = None
    mid_price = None
    has_wide_spread = None

    if yes_bid is not None and yes_ask is not None:
        spread = yes_ask - yes_bid
        mid_price = (yes_ask + yes_bid) / 2
        has_wide_spread = spread > Decimal("0.10")

    snapshot_count = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.market_pk == market.id)
        .count()
    )

    return {
        "market_id": market.market_id,
        "title": market.title,
        "status": market.status,
        "latest_snapshot_id": latest_snapshot.id,
        "latest_snapshot_ts": latest_snapshot.ts.isoformat() if latest_snapshot.ts else None,
        "yes_bid_dollars": decimal_to_float(latest_snapshot.yes_bid_dollars),
        "yes_ask_dollars": decimal_to_float(latest_snapshot.yes_ask_dollars),
        "last_price_dollars": decimal_to_float(latest_snapshot.last_price_dollars),
        "spread": decimal_to_float(spread),
        "mid_price": decimal_to_float(mid_price),
        "has_wide_spread": has_wide_spread,
        "volume_fp": decimal_to_float(latest_snapshot.volume_fp),
        "volume_24h_fp": decimal_to_float(latest_snapshot.volume_24h_fp),
        "open_interest_fp": decimal_to_float(latest_snapshot.open_interest_fp),
        "liquidity_dollars": decimal_to_float(latest_snapshot.liquidity_dollars),
        "snapshot_count": snapshot_count,
    }