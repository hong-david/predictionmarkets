import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import websockets
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.db.models import Market, MarketSnapshot, Trade
from app.db.session import SessionLocal
from app.services.anomaly_materializer import materialize_market_anomaly
from app.services.kalshi_auth import create_ws_headers


def parse_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def handle_ticker_message(data: dict) -> None:
    msg = data.get("msg", {})
    market_ticker = msg.get("market_ticker")
    if not market_ticker:
        return

    db = SessionLocal()
    try:
        market = db.query(Market).filter(Market.market_id == market_ticker).one_or_none()
        if market is None:
            print(f"Skipping unknown market_ticker={market_ticker}")
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
        market = db.query(Market).filter(Market.market_id == market_ticker).one_or_none()
        if market is None:
            print(f"Skipping trade for unknown market_ticker={market_ticker}")
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
        result = db.execute(stmt)
        db.commit()

        if result.rowcount:
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


async def consume_market_data_forever() -> None:
    backoff_seconds = 1

    while True:
        try:
            headers = create_ws_headers(
                settings.kalshi_api_key_id,
                settings.kalshi_private_key_path,
            )

            async with websockets.connect(
                settings.kalshi_ws_url,
                additional_headers=headers,
            ) as websocket:
                subscribe_message = {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["ticker", "trade"],
                    },
                }
                await websocket.send(json.dumps(subscribe_message))
                print("Connected and subscribed to ticker + trade channels.")
                backoff_seconds = 1

                async for raw_message in websocket:
                    data = json.loads(raw_message)
                    msg_type = data.get("type")

                    if msg_type == "ticker":
                        handle_ticker_message(data)
                    elif msg_type == "trade":
                        handle_trade_message(data)
                    elif msg_type == "error":
                        print(f"WebSocket error payload: {data}")
                    else:
                        print(f"Ignoring message type={msg_type}")

        except Exception as exc:
            print(f"WebSocket consumer error: {exc}. Reconnecting in {backoff_seconds}s...")
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 30)