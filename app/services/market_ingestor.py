from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import Market, MarketSnapshot


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_decimal(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(value)


def ingest_markets_payload(db: Session, payload: dict) -> dict[str, int]:
    markets = payload.get("markets", [])
    if not markets:
        return {
            "inserted_markets": 0,
            "updated_markets": 0,
            "snapshots_created": 0,
        }

    inserted = 0
    updated = 0
    snapshots_created = 0

    for item in markets:
        external_market_id = item["ticker"]

        market = (
            db.query(Market)
            .filter(Market.market_id == external_market_id)
            .one_or_none()
        )

        if market:
            market.event_id = item.get("event_ticker")
            market.ticker = item.get("ticker")
            market.title = item.get("title") or external_market_id
            market.subtitle = (
                item.get("yes_sub_title")
                or item.get("no_sub_title")
                or item.get("subtitle")
            )
            market.status = item.get("status") or "unknown"
            market.open_time = parse_dt(item.get("open_time"))
            market.close_time = parse_dt(item.get("close_time"))
            updated += 1
        else:
            market = Market(
                platform="kalshi",
                market_id=external_market_id,
                event_id=item.get("event_ticker"),
                ticker=item.get("ticker"),
                title=item.get("title") or external_market_id,
                subtitle=(
                    item.get("yes_sub_title")
                    or item.get("no_sub_title")
                    or item.get("subtitle")
                ),
                status=item.get("status") or "unknown",
                open_time=parse_dt(item.get("open_time")),
                close_time=parse_dt(item.get("close_time")),
            )
            db.add(market)
            db.flush()
            inserted += 1

        snapshot = MarketSnapshot(
            market_pk=market.id,
            last_price_dollars=parse_decimal(item.get("last_price_dollars")),
            yes_bid_dollars=parse_decimal(item.get("yes_bid_dollars")),
            yes_ask_dollars=parse_decimal(item.get("yes_ask_dollars")),
            no_bid_dollars=parse_decimal(item.get("no_bid_dollars")),
            no_ask_dollars=parse_decimal(item.get("no_ask_dollars")),
            volume_fp=parse_decimal(item.get("volume_fp")),
            volume_24h_fp=parse_decimal(item.get("volume_24h_fp")),
            open_interest_fp=parse_decimal(item.get("open_interest_fp")),
            liquidity_dollars=parse_decimal(item.get("liquidity_dollars")),
        )
        db.add(snapshot)
        snapshots_created += 1

    db.commit()

    return {
        "inserted_markets": inserted,
        "updated_markets": updated,
        "snapshots_created": snapshots_created,
    }