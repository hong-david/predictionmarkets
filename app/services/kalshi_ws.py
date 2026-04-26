import asyncio
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import websockets
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.db.models import BookEvent, Market, MarketSnapshot, Trade
from app.db.session import SessionLocal
from app.services.anomaly_materializer import materialize_market_anomaly
from app.services.classifier import CLASSIFIER_VERSION
from app.services.kalshi_auth import create_ws_headers
from app.services.retention import is_ticker_in_scope


def parse_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


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
            print(f"Failed to upsert market {market_ticker}; skipping ticker")
            return

        snapshot = MarketSnapshot(
            market_pk=market.id,
            last_price_dollars=parse_decimal(msg.get("last_price_dollars")),
            yes_bid_dollars=parse_decimal(msg.get("yes_bid_dollars")),
            yes_ask_dollars=parse_decimal(msg.get("yes_ask_dollars")),
            no_bid_dollars=parse_decimal(msg.get("no_bid_dollars")),
            no_ask_dollars=parse_decimal(msg.get("no_ask_dollars")),
            volume_fp=parse_decimal(msg.get("volume_fp")),
            volume_24h_fp=parse_decimal(msg.get("volume_24h_fp")),
            open_interest_fp=parse_decimal(msg.get("open_interest_fp")),
            liquidity_dollars=parse_decimal(msg.get("liquidity_dollars")),
        )
        db.add(snapshot)
        db.flush()

        materialize_market_anomaly(
            db,
            market,
            lookback=5,
            latest_snapshot_id=snapshot.id,
        )
        db.commit()

        print(
            f"ticker market={market.market_id} snapshot_id={snapshot.id} "
            f"yes_bid={snapshot.yes_bid_dollars} yes_ask={snapshot.yes_ask_dollars}"
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
        print(f"Skipping malformed trade message: {data}")
        return

    db = SessionLocal()
    try:
        market = _get_or_create_market(db, market_ticker)
        if market is None:
            print(f"Failed to upsert market {market_ticker}; skipping trade")
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
            print(
                f"trade market={market.market_id} trade_id={trade_id} "
                f"yes={msg.get('yes_price_dollars')} count_fp={msg.get('count_fp')} "
                f"side={msg.get('taker_side')}"
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
        print(f"Skipping malformed orderbook_snapshot: {data}")
        return

    db = SessionLocal()
    try:
        market = db.query(Market).filter(Market.market_id == market_ticker).one_or_none()
        if market is None:
            print(f"Skipping orderbook_snapshot for unknown market_ticker={market_ticker}")
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

        print(
            f"orderbook_snapshot market={market.market_id} seq={seq} "
            f"levels={len(rows)} session={session_id}"
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
        print(f"Skipping malformed orderbook_delta: {data}")
        return

    db = SessionLocal()
    try:
        market = db.query(Market).filter(Market.market_id == market_ticker).one_or_none()
        if market is None:
            print(f"Skipping orderbook_delta for unknown market_ticker={market_ticker}")
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
                print(
                    f"Subscribed to ticker + trade (session_id={session_id})."
                )

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
                    print(
                        f"Subscribed to orderbook_delta for {len(book_tickers)} markets."
                    )
                else:
                    print(
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
                        print(f"WebSocket error payload: {data}")
                    else:
                        print(f"Ignoring message type={msg_type}")

        except Exception as exc:
            print(f"WebSocket consumer error: {exc}. Reconnecting in {backoff_seconds}s...")
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 30)