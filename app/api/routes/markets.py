from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.api.serialization import decimal_to_float, isoformat_or_none
from app.db.models import Market, MarketSnapshot

router = APIRouter(
    prefix="/markets",
    tags=["markets (legacy)"],
    deprecated=True,
)


def serialize_market(market: Market) -> dict:
    return {
        "id": market.id,
        "platform": market.platform,
        "market_id": market.market_id,
        "event_id": market.event_id,
        "ticker": market.ticker,
        "title": market.title,
        "subtitle": market.subtitle,
        "status": market.status,
        "open_time": isoformat_or_none(market.open_time),
        "close_time": isoformat_or_none(market.close_time),
        "created_at": isoformat_or_none(market.created_at),
        "updated_at": isoformat_or_none(market.updated_at),
    }


def serialize_snapshot(snapshot: MarketSnapshot) -> dict:
    return {
        "id": snapshot.id,
        "market_pk": snapshot.market_pk,
        "ts": isoformat_or_none(snapshot.ts),
        "last_price_dollars": decimal_to_float(snapshot.last_price_dollars),
        "yes_bid_dollars": decimal_to_float(snapshot.yes_bid_dollars),
        "yes_ask_dollars": decimal_to_float(snapshot.yes_ask_dollars),
        "no_bid_dollars": decimal_to_float(snapshot.no_bid_dollars),
        "no_ask_dollars": decimal_to_float(snapshot.no_ask_dollars),
        "volume_fp": decimal_to_float(snapshot.volume_fp),
        "volume_24h_fp": decimal_to_float(snapshot.volume_24h_fp),
        "open_interest_fp": decimal_to_float(snapshot.open_interest_fp),
        "liquidity_dollars": decimal_to_float(snapshot.liquidity_dollars),
    }


@router.get(
    "",
    summary="List markets (legacy)",
    description="Prefer `GET /api/dashboard/markets` for classifier fields, scores, and filters.",
)
def list_markets(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    markets = db.query(Market).order_by(Market.id.desc()).limit(limit).all()
    return {
        "count": len(markets),
        "markets": [serialize_market(m) for m in markets],
    }


@router.get(
    "/{market_id}",
    summary="Get market (legacy)",
    description="Prefer `GET /api/dashboard/markets/{market_id}` for the detail bundle.",
)
def get_market(
    market_id: str,
    db: Session = Depends(get_db),
) -> dict:
    market = db.query(Market).filter(Market.market_id == market_id).one_or_none()
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")

    return serialize_market(market)


@router.get(
    "/{market_id}/snapshots",
    summary="Market snapshots (legacy)",
)
def get_market_snapshots(
    market_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    market = db.query(Market).filter(Market.market_id == market_id).one_or_none()
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")

    snapshots = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.market_pk == market.id)
        .order_by(MarketSnapshot.id.desc())
        .limit(limit)
        .all()
    )

    return {
        "market": serialize_market(market),
        "count": len(snapshots),
        "snapshots": [serialize_snapshot(s) for s in snapshots],
    }
