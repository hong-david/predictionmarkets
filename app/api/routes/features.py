from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.dependencies import get_db
from app.api.serialization import decimal_to_float, isoformat_or_none
from app.db.models import Market, MarketMetric
from app.services.quote_series import (
    display_price,
    latest_history_points,
    metric_point,
)

router = APIRouter(
    prefix="/markets",
    tags=["features (legacy)"],
    deprecated=True,
)


@router.get(
    "/{market_id}/features",
    summary="Per-market feature flags (legacy)",
    description="Prefer `GET /api/dashboard/markets/{market_id}` for a richer read bundle.",
)
def get_market_features(
    market_id: str,
    db: Session = Depends(get_db),
) -> dict:
    market = db.query(Market).filter(Market.market_id == market_id).one_or_none()
    if market is None:
        raise HTTPException(status_code=404, detail="Market not found")

    metric = (
        db.query(MarketMetric)
        .filter(MarketMetric.market_pk == market.id)
        .one_or_none()
    )
    quote = metric_point(metric) if metric is not None else None
    if quote is None or display_price(quote) is None:
        rows = latest_history_points(db, [market.id]).get(int(market.id), [])
        quote = rows[0] if rows else quote

    if quote is None:
        raise HTTPException(status_code=404, detail="No quote history found for market")

    yes_bid = quote.yes_bid_dollars
    yes_ask = quote.yes_ask_dollars

    spread = None
    mid_price = None
    has_wide_spread = None

    if yes_bid is not None and yes_ask is not None:
        spread = yes_ask - yes_bid
        mid_price = (yes_ask + yes_bid) / 2
        has_wide_spread = spread > Decimal("0.10")

    history_count = len(
        latest_history_points(db, [market.id], limit_per_market=100).get(
            int(market.id), []
        )
    )

    return {
        "market_id": market.market_id,
        "title": market.title,
        "status": market.status,
        "latest_snapshot_id": None,
        "latest_snapshot_ts": isoformat_or_none(quote.ts),
        "latest_quote_source": quote.source,
        "yes_bid_dollars": decimal_to_float(quote.yes_bid_dollars),
        "yes_ask_dollars": decimal_to_float(quote.yes_ask_dollars),
        "last_price_dollars": decimal_to_float(quote.last_price_dollars),
        "spread": decimal_to_float(spread),
        "mid_price": decimal_to_float(mid_price),
        "has_wide_spread": has_wide_spread,
        "volume_fp": decimal_to_float(quote.volume_fp),
        "volume_24h_fp": decimal_to_float(quote.volume_24h_fp),
        "open_interest_fp": decimal_to_float(quote.open_interest_fp),
        "liquidity_dollars": decimal_to_float(quote.liquidity_dollars),
        "snapshot_count": history_count,
        "history_count_sample": history_count,
    }
