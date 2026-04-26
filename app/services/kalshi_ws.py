import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import websockets
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.db.models import Anomaly, BookEvent, Market, MarketSnapshot, Trade
from app.db.session import SessionLocal
from app.services.anomaly_materializer import materialize_market_anomaly
from app.services.classifier import CLASSIFIER_VERSION
from app.services.decimal_utils import parse_decimal
from app.services.kalshi_auth import create_ws_headers
from app.services.retention import is_ticker_in_scope, should_persist_raw_tape
from app.services.snapshot_dedup import should_skip_duplicate_snapshot

logger = logging.getLogger(__name__)

# Latest volume_24h / open interest hints from the ticker channel (or DB fallback).
# Used to gate which markets persist raw trades and book_event rows.
_TAPE_HINT_TTL_SEC = 120.0
_tape_volume_cache: dict[int, tuple[Decimal | None, Decimal | None, float]] = {}


def _update_tape_hints_from_ticker(
    market_pk: int,
    v24: Decimal | None,
    oi: Decimal | None,
) -> None:
    _tape_volume_cache[market_pk] = (v24, oi, time.monotonic())


def _tape_hints_for_market(db, market: Market) -> tuple[Decimal | None, Decimal | None]:
    now_m = time.monotonic()
    ent = _tape_volume_cache.get(market.id)
    if ent and (now_m - ent[2]) < _TAPE_HINT_TTL_SEC:
        return ent[0], ent[1]
    last = (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.market_pk == market.id)
        .order_by(MarketSnapshot.id.desc())
        .first()
    )
    v24 = last.volume_24h_fp if last else None
    oi = last.open_interest_fp if last else None
    _tape_volume_cache[market.id] = (v24, oi, now_m)
    return v24, oi


def _raw_tape_allowed(db, market: Market) -> bool:
    """True if this market may append trades / book_event rows in this process."""
    v24, oi = _tape_hints_for_market(db, market)
    if should_persist_raw_tape(
        market,
        volume_24h_fp=v24,
        open_interest_fp=oi,
        has_materialized_anomaly=False,
    ):
        return True
    return (
        db.query(Anomaly.id)
        .filter(Anomaly.market_pk == market.id)
        .limit(1)
        .first()
        is not None
    )


def _get_or_create_market(db, market_ticker: str) -> Market | None:
    """Idempotently fetch (or stub-create) a Market row for `market_ticker`.

    The Kalshi `/markets` endpoint paginates alphabetically across tens of
    thousands of markets, so a bounded one-shot backfill cannot guarantee the
    actively-trading mainstream markets are present at consumer startup.
    Rather than dropping every WS message for an unknown ticker, we
    auto-create a stub Market row carrying `status='unknown'` as a sentinel
    that means "seen on the wire, REST metadata not yet hydrated". The REST
    poller is the single writer that fills in title, event_ticker, open/close
    times, etc.

    Concurrency: this uses Postgres `INSERT ... ON CONFLICT DO NOTHING`
    against the `markets.market_id` unique constraint, so concurrent inserts
    of the same ticker race safely. The follow-up SELECT then returns
    whichever row won.
    """
    market = db.query(Market).filter(Market.market_id == market_ticker).one_or_none()
    if market is not None:
        return market

    # Scope filter for never-before-seen tickers. We only have the
    # ticker string here (no title, no Kalshi metadata), so the
    # classifier runs Layer 2 (prefix rules) only. That's enough to
    # catch the obvious noise — `KXMVECROSSCATEGORY-...` prefixes match
    # `exotic.cross_category` and get rejected before we ever insert.
    # If the ticker matches no rule, we keep it (Layer 2's fallback is
    # `other.unclassified` with prior `medium`); the REST poller will
    # later hydrate metadata and re-classify with full information.
    in_scope, classification = is_ticker_in_scope(market_ticker)
    if not in_scope:
        return None

    stmt = (
        pg_insert(Market)
        .values(
            platform="kalshi",
            market_id=market_ticker,
            title=market_ticker,
            status="unknown",
            # Stamp the ticker-only classification onto the stub. The
            # REST poller will overwrite this on the next sweep with the
            # richer Layer 1 / 3 verdict.
            category=classification.category,
            subcategory=classification.subcategory,
            manipulability_prior=classification.manipulability_prior,
            classifier_tags=list(classification.tags),
            classifier_layer=classification.layer,
            classifier_rule=classification.rule,
            classifier_confidence=classification.confidence,
            classifier_version=CLASSIFIER_VERSION,
        )
        .on_conflict_do_nothing(index_elements=["market_id"])
    )
    db.execute(stmt)
    return db.query(Market).filter(Market.market_id == market_ticker).one_or_none()


def handle_ticker_message(data: dict) -> None:
    msg = data.get("msg", {})
    market_ticker = msg.get("market_ticker")
    if not market_ticker:
        return

    db = SessionLocal()
    try:
        market = _get_or_create_market(db, market_ticker)
        if market is None:
            logger.info("Skipping out-of-scope ticker message for %s", market_ticker)
            return

        lp = parse_decimal(msg.get("last_price_dollars"))
        yb = parse_decimal(msg.get("yes_bid_dollars"))
        ya = parse_decimal(msg.get("yes_ask_dollars"))
        nb = parse_decimal(msg.get("no_bid_dollars"))
        na = parse_decimal(msg.get("no_ask_dollars"))
        vol = parse_decimal(msg.get("volume_fp"))
        v24 = parse_decimal(msg.get("volume_24h_fp"))
        oi = parse_decimal(msg.get("open_interest_fp"))
        liq = parse_decimal(msg.get("liquidity_dollars"))
        _update_tape_hints_from_ticker(market.id, v24, oi)
        if should_skip_duplicate_snapshot(
            db,
            market.id,
            last_price_dollars=lp,
            yes_bid_dollars=yb,
            yes_ask_dollars=ya,
            no_bid_dollars=nb,
            no_ask_dollars=na,
            volume_fp=vol,
            volume_24h_fp=v24,
            open_interest_fp=oi,
            liquidity_dollars=liq,
        ):
            return

        snapshot = MarketSnapshot(
            market_pk=market.id,
            last_price_dollars=lp,
            yes_bid_dollars=yb,
            yes_ask_dollars=ya,
            no_bid_dollars=nb,
            no_ask_dollars=na,
            volume_fp=vol,
            volume_24h_fp=v24,
            open_interest_fp=oi,
            liquidity_dollars=liq,
        )
        db.add(snapshot)
        db.flush()

        materialize_market_anomaly(
            db,
            market,
            lookback=40,
            latest_snapshot_id=snapshot.id,
        )
        db.commit()

        logger.info(
            "ticker market=%s snapshot_id=%s yes_bid=%s yes_ask=%s",
            market.market_id,
            snapshot.id,
            snapshot.yes_bid_dollars,
            snapshot.yes_ask_dollars,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def handle_trade_message(data: dict) -> None:
    msg = data.get("msg", {})
    market_ticker = msg.get("market_ticker")
    trade_id = msg.get("trade_id")
    ts_ms = msg.get("ts_ms")

    if not market_ticker or not trade_id or ts_ms is None:
        logger.warning("Skipping malformed trade message: %s", data)
        return

    db = SessionLocal()
    try:
        market = _get_or_create_market(db, market_ticker)
        if market is None:
            logger.info("Skipping out-of-scope trade message for %s", market_ticker)
            return

        if not _raw_tape_allowed(db, market):
            # Lazy-upsert may have just inserted a stub `Market`; commit so it
            # survives even when we drop the trade on the floor (raw-tape gate).
            db.commit()
            return

        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

        # ON CONFLICT DO NOTHING keeps reconnect-replays cheap and avoids
        # poisoning the transaction with IntegrityErrors per duplicate.
        stmt = (
            pg_insert(Trade)
            .values(
                market_pk=market.id,
                trade_id=trade_id,
                ts=ts,
                yes_price_dollars=parse_decimal(msg.get("yes_price_dollars")),
                no_price_dollars=parse_decimal(msg.get("no_price_dollars")),
                count_fp=parse_decimal(msg.get("count_fp")),
                taker_side=msg.get("taker_side"),
            )
            .on_conflict_do_nothing(index_elements=["trade_id"])
        )
        # SQLAlchemy 2's static return type is `Result[Any]`, but a DML
        # statement executes as a `CursorResult` at runtime, which is what
        # exposes `rowcount`. Suppress the false-positive attribute error.
        result = db.execute(stmt)
        db.commit()

        if result.rowcount:  # type: ignore[attr-defined]
            logger.info(
                "trade market=%s trade_id=%s yes=%s count_fp=%s side=%s",
                market.market_id,
                trade_id,
                msg.get("yes_price_dollars"),
                msg.get("count_fp"),
                msg.get("taker_side"),
            )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _ts_from_ms(ts_ms) -> datetime | None:
    if ts_ms is None:
        return None
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def handle_orderbook_snapshot_message(data: dict, session_id: str) -> None:
    msg = data.get("msg", {})
    market_ticker = msg.get("market_ticker")
    seq = data.get("seq")

    if not market_ticker or seq is None:
        logger.warning("Skipping malformed orderbook_snapshot: %s", data)
        return

    db = SessionLocal()
    try:
        market = db.query(Market).filter(Market.market_id == market_ticker).one_or_none()
        if market is None:
            logger.warning(
                "Skipping orderbook_snapshot for unknown market_ticker=%s",
                market_ticker,
            )
            return

        if not _raw_tape_allowed(db, market):
            return

        ts = _ts_from_ms(msg.get("ts_ms"))

        rows: list[dict] = []
        for side, key in (("yes", "yes_dollars_fp"), ("no", "no_dollars_fp")):
            for level in msg.get(key) or []:
                if not isinstance(level, (list, tuple)) or len(level) < 2:
                    continue
                price_str, size_str = level[0], level[1]
                rows.append(
                    {
                        "market_pk": market.id,
                        "session_id": session_id,
                        "seq": seq,
                        "ts": ts,
                        "side": side,
                        "price_dollars": parse_decimal(price_str),
                        "size_fp": parse_decimal(size_str),
                        "delta_fp": None,
                        "is_snapshot": True,
                    }
                )

        if not rows:
            return

        # Bulk insert: one statement, ON CONFLICT DO NOTHING keeps re-applying
        # the same snapshot idempotent across reconnect-replays.
        stmt = (
            pg_insert(BookEvent)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["session_id", "seq", "side", "price_dollars"]
            )
        )
        db.execute(stmt)
        db.commit()

        logger.info(
            "orderbook_snapshot market=%s seq=%s levels=%s session=%s",
            market.market_id,
            seq,
            len(rows),
            session_id,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def handle_orderbook_delta_message(data: dict, session_id: str) -> None:
    msg = data.get("msg", {})
    market_ticker = msg.get("market_ticker")
    seq = data.get("seq")
    side = msg.get("side")
    price_str = msg.get("price_dollars")
    delta_str = msg.get("delta_fp")

    if (
        not market_ticker
        or seq is None
        or side is None
        or price_str is None
        or delta_str is None
    ):
        logger.warning("Skipping malformed orderbook_delta: %s", data)
        return

    db = SessionLocal()
    try:
        market = db.query(Market).filter(Market.market_id == market_ticker).one_or_none()
        if market is None:
            logger.warning(
                "Skipping orderbook_delta for unknown market_ticker=%s",
                market_ticker,
            )
            return

        if not _raw_tape_allowed(db, market):
            return

        ts = _ts_from_ms(msg.get("ts_ms"))

        stmt = (
            pg_insert(BookEvent)
            .values(
                market_pk=market.id,
                session_id=session_id,
                seq=seq,
                ts=ts,
                side=side,
                price_dollars=parse_decimal(price_str),
                size_fp=None,
                delta_fp=parse_decimal(delta_str),
                is_snapshot=False,
            )
            .on_conflict_do_nothing(
                index_elements=["session_id", "seq", "side", "price_dollars"]
            )
        )
        db.execute(stmt)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def resolve_book_market_tickers() -> list[str]:
    """Resolve which markets to subscribe to orderbook_delta for.

    Order of precedence:
      1. Explicit `kalshi_book_market_tickers` from settings (operator override).
      2. The most actively-trading markets, ranked by `volume_fp` (lifetime
         cumulative volume) from each market's most recent snapshot. We use
         lifetime rather than 24h volume because the Kalshi WS ticker
         payload does not include `volume_24h_fp` -- only `volume_fp` -- so
         a 24h ranker would silently drop every market we have only seen
         via WS, which is exactly the actively-trading set.
      3. Cold-start fallback: most-recently-updated markets, used only when
         no snapshots with positive volume exist yet (e.g. brand-new DB).

    Smoke-test war story: the previous implementation was just (1) -> (3).
    On a fresh bootstrap of 50k markets, Kalshi's alphabetical pagination
    front-loaded ~50k dead exotic combinatorial markets, so step (3) picked
    50 of those and we got zero `orderbook_snapshot` events for a full
    minute against live Kalshi. Volume-ranking fixes that even when the
    bootstrap is biased.

    The DISTINCT ON (market_pk) ... ORDER BY market_pk, ts DESC pattern is
    Postgres-native and yields one (latest) snapshot per market in a single
    index pass. We treat 'active', 'open', and 'unknown' as tradeable since
    'unknown' is the sentinel set by the lazy-upsert path for markets we've
    only seen via the WS feed.
    """
    if settings.kalshi_book_market_tickers:
        return list(settings.kalshi_book_market_tickers)

    db = SessionLocal()
    try:
        latest_per_market = (
            select(
                MarketSnapshot.market_pk.label("market_pk"),
                MarketSnapshot.volume_fp.label("vol"),
            )
            .order_by(MarketSnapshot.market_pk, MarketSnapshot.ts.desc())
            .distinct(MarketSnapshot.market_pk)
            .subquery()
        )

        ranked = db.execute(
            select(Market.market_id)
            .join(latest_per_market, latest_per_market.c.market_pk == Market.id)
            .where(Market.status.in_(["active", "open", "unknown"]))
            .where(latest_per_market.c.vol.is_not(None))
            .where(latest_per_market.c.vol > 0)
            .order_by(latest_per_market.c.vol.desc())
            .limit(settings.kalshi_book_market_limit)
        ).all()

        if ranked:
            return [r[0] for r in ranked]

        rows = (
            db.query(Market.market_id)
            .filter(Market.status.in_(["active", "open"]))
            .order_by(Market.updated_at.desc())
            .limit(settings.kalshi_book_market_limit)
            .all()
        )
        return [r[0] for r in rows]
    finally:
        db.close()


async def consume_market_data_forever() -> None:
    backoff_seconds = 1

    while True:
        try:
            session_id = str(uuid.uuid4())
            headers = create_ws_headers(
                settings.kalshi_api_key_id,
                settings.kalshi_private_key_path,
            )

            async with websockets.connect(
                settings.kalshi_ws_url,
                additional_headers=headers,
            ) as websocket:
                # Subscribe 1: ticker + trade for all markets (no market filter
                # is supported on these channels).
                await websocket.send(
                    json.dumps(
                        {
                            "id": 1,
                            "cmd": "subscribe",
                            "params": {"channels": ["ticker", "trade"]},
                        }
                    )
                )
                logger.info("Subscribed to ticker + trade (session_id=%s).", session_id)

                # Subscribe 2: orderbook_delta for the selected markets only.
                # Sent as a separate command because it needs a different
                # `params` shape (market_tickers).
                book_tickers = resolve_book_market_tickers()
                if book_tickers:
                    await websocket.send(
                        json.dumps(
                            {
                                "id": 2,
                                "cmd": "subscribe",
                                "params": {
                                    "channels": ["orderbook_delta"],
                                    "market_tickers": book_tickers,
                                },
                            }
                        )
                    )
                    logger.info(
                        "Subscribed to orderbook_delta for %s markets.",
                        len(book_tickers),
                    )
                else:
                    logger.info(
                        "No markets resolved for orderbook_delta; skipping subscription."
                    )

                backoff_seconds = 1

                async for raw_message in websocket:
                    data = json.loads(raw_message)
                    msg_type = data.get("type")

                    if msg_type == "ticker":
                        handle_ticker_message(data)
                    elif msg_type == "trade":
                        handle_trade_message(data)
                    elif msg_type == "orderbook_snapshot":
                        handle_orderbook_snapshot_message(data, session_id)
                    elif msg_type == "orderbook_delta":
                        handle_orderbook_delta_message(data, session_id)
                    elif msg_type == "error":
                        logger.warning("WebSocket error payload: %s", data)
                    else:
                        logger.debug("Ignoring message type=%s", msg_type)

        except Exception as exc:
            logger.warning(
                "WebSocket consumer error: %s. Reconnecting in %ss...",
                exc,
                backoff_seconds,
            )
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 30)
