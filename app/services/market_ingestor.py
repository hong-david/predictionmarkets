from datetime import datetime
from decimal import Decimal
from itertools import islice
from typing import Iterable, Iterator, TypeVar

from sqlalchemy.orm import Session

from app.db.models import Market, MarketSnapshot
from app.services.classifier import CLASSIFIER_VERSION, classify
from app.services.retention import is_market_in_scope

T = TypeVar("T")


def _stamp_classification(market: Market, result) -> None:
    """Copy a `Classification` onto a `Market` row in one place.

    Pulled out so both the lazy-skip and the freshly-classified paths
    write the same set of columns.
    """
    market.category = result.category
    market.subcategory = result.subcategory
    market.manipulability_prior = result.manipulability_prior
    market.classifier_tags = list(result.tags)
    market.classifier_layer = result.layer
    market.classifier_rule = result.rule
    market.classifier_confidence = result.confidence
    market.classifier_version = CLASSIFIER_VERSION


def _apply_classification(market: Market, item: dict) -> None:
    """Run the classifier against the raw Kalshi `item` and stamp the result.

    Lazy: skips work when the row is already at `CLASSIFIER_VERSION`. That
    short-circuit is important under the 5-minute polling cadence — most
    rows are unchanged between sweeps and we don't want to re-run the
    full layer stack (especially Layer 3 / 4) on every poll.

    The classifier never raises; if every layer abstains it produces a
    `("other", "unclassified")` fallback, so the columns are always
    populated after this call.
    """
    if market.classifier_version == CLASSIFIER_VERSION:
        return
    result = classify(market=item)
    _stamp_classification(market, result)


def chunked(iterable: Iterable[T], size: int) -> Iterator[list[T]]:
    """Yield successive `size`-element lists from `iterable`.

    Lives next to `ingest_markets_payload` because every caller pairs the
    two: walk Kalshi's paginated cursor, then ingest in fixed-size batches
    so we can commit progressively rather than buffering tens of thousands
    of rows in memory before any DB write happens.
    """
    if size < 1:
        raise ValueError("size must be >= 1")
    it = iter(iterable)
    while True:
        batch = list(islice(it, size))
        if not batch:
            return
        yield batch


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
            "skipped_out_of_scope": 0,
        }

    inserted = 0
    updated = 0
    snapshots_created = 0
    skipped_out_of_scope = 0

    for item in markets:
        external_market_id = item["ticker"]

        # Scope filter — runs *before* any DB I/O so out-of-scope markets
        # never produce a SELECT, an UPSERT, or a snapshot row. The
        # classification we computed here is reused below if the market
        # is in scope, so the filter is free in the keep-path.
        in_scope, classification = is_market_in_scope(item)
        if not in_scope:
            # If the market already exists in the DB and *was* in scope
            # at some earlier ingest (e.g. a category we used to track
            # and have since removed), we leave the row alone. Removal
            # is the prune script's job, not the ingestor's — that
            # separation keeps the ingestor side-effect-light and makes
            # accidental data loss impossible from a poller misconfig.
            skipped_out_of_scope += 1
            continue

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

        # Reuse the classification we already computed for the scope
        # check rather than running the four-layer stack a second time.
        # Same lazy-skip rule as `_apply_classification`.
        if market.classifier_version != CLASSIFIER_VERSION:
            _stamp_classification(market, classification)

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
        "skipped_out_of_scope": skipped_out_of_scope,
    }