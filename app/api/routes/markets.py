from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.db.models import Market, MarketSnapshot

router = APIRouter(prefix="/markets", tags=["markets"])


def serialize_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize_decimal(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


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
        "open_time": serialize_datetime(market.open_time),
        "close_time": serialize_datetime(market.close_time),
        "created_at": serialize_datetime(market.created_at),
        "updated_at": serialize_datetime(market.updated_at),
    }


def serialize_snapshot(snapshot: MarketSnapshot) -> dict:
    return {
        "id": snapshot.id,
        "market_pk": snapshot.market_pk,
        "ts": serialize_datetime(snapshot.ts),
        "last_price_dollars": serialize_decimal(snapshot.last_price_dollars),
        "yes_bid_dollars": serialize_decimal(snapshot.yes_bid_dollars),
        "yes_ask_dollars": serialize_decimal(snapshot.yes_ask_dollars),
        "no_bid_dollars": serialize_decimal(snapshot.no_bid_dollars),
        "no_ask_dollars": serialize_decimal(snapshot.no_ask_dollars),
        "volume_fp": serialize_decimal(snapshot.volume_fp),
        "volume_24h_fp": serialize_decimal(snapshot.volume_24h_fp),
        "open_interest_fp": serialize_decimal(snapshot.open_interest_fp),
        "liquidity_dollars": serialize_decimal(snapshot.liquidity_dollars),
    }


@router.get("")
def list_markets(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    markets = db.query(Market).order_by(Market.id.desc()).limit(limit).all()
    return {
        "count": len(markets),
        "markets": [serialize_market(m) for m in markets],
    }


@router.get("/{market_id}")
def get_market(
    market_id: str,
    db: Session = Depends(get_db),
) -> dict:
    market = db.query(Market).filter(Market.market_id == market_id).one_or_none()
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")

    return serialize_market(market)


@router.get("/{market_id}/snapshots")
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