import asyncio
import json
import logging
import os
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import websockets
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.db.models import (
    Anomaly,
    BookEvent,
    Market,
    MarketMetric,
    MarketSnapshot,
    Trade,
    TradeFlag,
)
from app.db.session import SessionLocal
from app.services.anomaly_materializer import materialize_market_anomaly
from app.services.classifier import CLASSIFIER_VERSION
from app.services.decimal_utils import parse_decimal
from app.services.kalshi_auth import create_ws_headers
from app.services.clickhouse_writer import clickhouse_batcher
from app.services.market_metrics import (
    bulk_upsert_quote_metrics,
    bump_trade_metrics,
    quote_metric_values,
    upsert_quote_metrics,
)
from app.services.market_price_history import (
    bulk_upsert_chart_history,
    quote_history_row,
    should_write_quote_history,
    trade_history_row,
    upsert_chart_history,
)
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    new_run_id,
    record_pipeline_heartbeat,
)
from app.services.retention import (
    EXCLUDED_CATEGORIES,
    EXCLUDED_PRIORS,
    RetentionSignals,
    StorageDecision,
    is_ticker_in_scope,
    should_sample_event,
    storage_decision_for_event,
)
from app.services.snapshot_dedup import (
    should_skip_duplicate_snapshot,
    snapshot_row_is_duplicate,
)

logger = logging.getLogger(__name__)

# Latest volume_24h / open interest hints from the ticker channel (or DB fallback).
# Used to gate which markets persist raw trades and book_event rows.
_TAPE_HINT_TTL_SEC = 120.0
_tape_volume_cache: dict[int, tuple[Decimal | None, Decimal | None, float]] = {}
_CH_TRADES_TABLE = "kalshi_trades_raw"
_CH_QUOTES_TABLE = "kalshi_quote_changes_raw"
_CH_L2_TABLE = "kalshi_l2_events_raw"
_BOOK_MARKET_ACTIVITY_RECENCY_MINUTES = max(
    1,
    int(os.getenv("KALSHI_BOOK_MARKET_ACTIVITY_RECENCY_MINUTES", "30")),
)

def _env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        logger.warning("Invalid %s=%r; using minimum %s", name, raw, minimum)
        return minimum
    if maximum is not None and value > maximum:
        logger.warning("Invalid %s=%r; using maximum %s", name, raw, maximum)
        return maximum
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


_WS_TICKER_COALESCE_ENABLED = _env_bool("KALSHI_WS_TICKER_COALESCE_ENABLED", True)
_WS_TICKER_COALESCE_WINDOW_SEC = _env_float(
    "KALSHI_WS_TICKER_COALESCE_WINDOW_SEC",
    0.25,
    minimum=0.05,
)
_WS_TICKER_FLUSH_MAX_QUEUE_FRACTION = _env_float(
    "KALSHI_WS_TICKER_FLUSH_MAX_QUEUE_FRACTION",
    0.8,
    minimum=0.0,
    maximum=1.0,
)
_WS_METRICS_LOG_INTERVAL_SEC = _env_float(
    "KALSHI_WS_METRICS_LOG_INTERVAL_SEC",
    30.0,
    minimum=1.0,
)
_WS_TICKER_BATCH_DB_WRITES_ENABLED = _env_bool(
    "KALSHI_WS_TICKER_BATCH_DB_WRITES_ENABLED",
    True,
)
_WS_TICKER_METRICS_WRITE_GATE_ENABLED = _env_bool(
    "KALSHI_WS_TICKER_METRICS_WRITE_GATE_ENABLED",
    True,
)
_WS_TICKER_METRICS_MIN_INTERVAL_SEC = _env_float(
    "KALSHI_WS_TICKER_METRICS_MIN_INTERVAL_SEC",
    5.0,
    minimum=0.0,
)
_WS_TICKER_KNOWN_MARKET_GATE_ENABLED = _env_bool(
    "KALSHI_WS_TICKER_KNOWN_MARKET_GATE_ENABLED",
    True,
)
_WS_TICKER_KNOWN_MARKET_REFRESH_SEC = _env_float(
    "KALSHI_WS_TICKER_KNOWN_MARKET_REFRESH_SEC",
    300.0,
    minimum=30.0,
)
_WS_TICKER_SNAPSHOTS_ENABLED = _env_bool(
    "KALSHI_WS_TICKER_SNAPSHOTS_ENABLED",
    True,
)

_WS_METRICS: Counter[str] = Counter()
_WS_METRICS_STARTED_AT = time.monotonic()
_WS_METRIC_KEYS = (
    "ticker_pending_new",
    "ticker_coalesced",
    "ticker_flushed",
    "ticker_flush_deferred",
    "ticker_batch_processed",
    "ticker_batch_messages",
    "ticker_known_market_gate_dropped",
    "ticker_known_market_refresh_errors",
    "ticker_unknown_skipped",
    "ticker_snapshots_inserted",
    "ticker_snapshots_skipped_disabled",
    "ticker_snapshots_skipped_duplicate",
    "ticker_snapshots_skipped_sampling",
    "ticker_metrics_upserted",
    "ticker_metrics_skipped_gate",
)

_KNOWN_MARKET_TICKERS: set[str] = set()
_KNOWN_MARKET_LAST_REFRESH = 0.0

# Market-state anomaly scoring needs the recent snapshot window, so running it
# after every sampled ticker snapshot creates a high-volume
# `market_snapshots ... order by ts desc limit 40` workload. Keep hot/high-signal
# markets immediate, but throttle lower-signal tiers per process.
_ANOMALY_MATERIALIZE_LAST_RUN: dict[int, float] = {}
_ANOMALY_MATERIALIZE_ALWAYS_TIERS = {"hot", "triggered", "case"}
_ANOMALY_MATERIALIZE_INTERVAL_BY_TIER_SEC = {
    "sampled": 1800.0,
    "observe_only": 3600.0,
}
_ANOMALY_MATERIALIZE_DEFAULT_INTERVAL_SEC = 1800.0
_FULL_TICKER_SNAPSHOT_TIERS = {"hot", "triggered", "case"}


def first_present(*values):
    """Return first non-null/non-empty value without treating 0 as missing."""
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _should_materialize_market_anomaly(
    market_pk: int,
    decision: StorageDecision,
) -> bool:
    tier = (decision.tier or "").strip()
    if tier in _ANOMALY_MATERIALIZE_ALWAYS_TIERS:
        return True

    interval = _ANOMALY_MATERIALIZE_INTERVAL_BY_TIER_SEC.get(
        tier,
        _ANOMALY_MATERIALIZE_DEFAULT_INTERVAL_SEC,
    )
    now_m = time.monotonic()
    last = _ANOMALY_MATERIALIZE_LAST_RUN.get(market_pk)
    if last is not None and (now_m - last) < interval:
        return False

    _ANOMALY_MATERIALIZE_LAST_RUN[market_pk] = now_m
    return True


def _should_store_ticker_snapshot(
    market: Market,
    row_key: str,
    decision: StorageDecision,
) -> bool:
    """Store full quote evidence for hot tiers; sample lower tiers."""
    tier = (decision.tier or "").strip()
    if tier in _FULL_TICKER_SNAPSHOT_TIERS:
        return True
    return should_sample_event(row_key, decision.sample_rate)



def _raw_backend() -> str:
    backend = settings.kalshi_raw_backend.lower().strip()
    if backend not in {"postgres", "clickhouse", "dual"}:
        logger.warning(
            "Unknown kalshi_raw_backend=%s; falling back to postgres", backend
        )
        return "postgres"
    return backend


def _write_raw_postgres() -> bool:
    return _raw_backend() in {"postgres", "dual"}


def _write_raw_clickhouse() -> bool:
    return _raw_backend() in {"clickhouse", "dual"}


def _cents(value: Decimal | None) -> int:
    if value is None:
        return 0
    return max(0, min(255, int((value * Decimal("100")).to_integral_value())))


def _contracts(value: Decimal | None) -> int:
    if value is None:
        return 0
    return max(0, int(value.to_integral_value()))


def _signed_contracts(value: Decimal | None) -> int:
    if value is None:
        return 0
    return int(value.to_integral_value())


def _side_enum(value: object) -> str:
    side = str(value or "").lower()
    return side if side in {"yes", "no"} else "unknown"


def _clickhouse_ts(value: datetime | None = None) -> datetime:
    return value or datetime.now(timezone.utc)



def _ws_msg_type(data: dict) -> str:
    return str(data.get("type") or "unknown")


def _ticker_market_key(data: dict) -> str | None:
    if data.get("type") != "ticker":
        return None
    msg = data.get("msg") or {}
    market_ticker = msg.get("market_ticker")
    return str(market_ticker) if market_ticker else None


def _refresh_known_market_tickers() -> int:
    """Refresh the ticker allowlist used to keep ticker-only WS traffic cheap.

    Trades and orderbook events still use the lazy market path; this allowlist is
    only for ticker state, where dropping an unknown first sighting is acceptable
    because the REST poller owns discovery and hydration. Keep the allowlist to
    tradeable in-scope rows so historical tables do not turn into a large
    resident-memory set in long-running workers.
    """
    global _KNOWN_MARKET_LAST_REFRESH, _KNOWN_MARKET_TICKERS
    db = SessionLocal()
    try:
        rows = (
            db.query(Market.market_id, Market.ticker)
            .filter(Market.status.in_(["active", "open", "unknown"]))
            .filter(
                or_(
                    Market.category.is_(None),
                    Market.category.notin_(tuple(EXCLUDED_CATEGORIES)),
                )
            )
            .filter(
                or_(
                    Market.manipulability_prior.is_(None),
                    Market.manipulability_prior.notin_(tuple(EXCLUDED_PRIORS)),
                )
            )
            .all()
        )
        tickers: set[str] = set()
        for market_id, ticker in rows:
            if market_id:
                tickers.add(str(market_id))
            if ticker:
                tickers.add(str(ticker))
        _KNOWN_MARKET_TICKERS = tickers
        _KNOWN_MARKET_LAST_REFRESH = time.monotonic()
        return len(tickers)
    finally:
        db.close()


def _ticker_allowed_by_known_market_gate(market_ticker: str) -> bool:
    if not _WS_TICKER_KNOWN_MARKET_GATE_ENABLED:
        return True
    # Fail open before the first successful refresh; dropping every ticker on a
    # transient DB read failure would be worse than doing the old per-ticker path.
    if not _KNOWN_MARKET_TICKERS:
        return True
    return market_ticker in _KNOWN_MARKET_TICKERS


async def _known_market_ticker_worker(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            count = await asyncio.to_thread(_refresh_known_market_tickers)
            logger.info("Refreshed WS ticker known-market allowlist: %s tickers", count)
        except Exception:
            _WS_METRICS["ticker_known_market_refresh_errors"] += 1
            logger.exception("Failed to refresh WS ticker known-market allowlist")

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=max(30.0, _WS_TICKER_KNOWN_MARKET_REFRESH_SEC),
            )
        except asyncio.TimeoutError:
            pass


async def _flush_ticker_coalescer(
    *,
    pending_tickers: dict[str, dict],
    pending_lock: asyncio.Lock,
    queue: asyncio.Queue[dict | None],
    force: bool = False,
) -> int:
    maxsize = queue.maxsize
    if (
        not force
        and maxsize > 0
        and queue.qsize() >= int(maxsize * _WS_TICKER_FLUSH_MAX_QUEUE_FRACTION)
    ):
        async with pending_lock:
            pending_count = len(pending_tickers)
        if pending_count:
            _WS_METRICS["ticker_flush_deferred"] += 1
        return 0

    async with pending_lock:
        if not pending_tickers:
            return 0
        batch = list(pending_tickers.values())
        pending_tickers.clear()

    if _WS_TICKER_BATCH_DB_WRITES_ENABLED:
        await queue.put({"type": "_ticker_batch", "messages": batch})
    else:
        for item in batch:
            await queue.put(item)

    _WS_METRICS["ticker_flushed"] += len(batch)
    _WS_METRICS["queued_ticker"] += len(batch)
    return len(batch)


async def _ticker_coalescer_worker(
    *,
    pending_tickers: dict[str, dict],
    pending_lock: asyncio.Lock,
    queue: asyncio.Queue[dict | None],
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(max(0.05, _WS_TICKER_COALESCE_WINDOW_SEC))
        await _flush_ticker_coalescer(
            pending_tickers=pending_tickers,
            pending_lock=pending_lock,
            queue=queue,
        )

    await _flush_ticker_coalescer(
        pending_tickers=pending_tickers,
        pending_lock=pending_lock,
        queue=queue,
        force=True,
    )


async def _dispatch_ws_message(data: dict, session_id: str) -> None:
    msg_type = data.get("type")
    if msg_type == "_ticker_batch":
        messages = data.get("messages") or []
        _WS_METRICS["handled_ticker"] += len(messages)
        _WS_METRICS["ticker_batch_processed"] += 1
        _WS_METRICS["ticker_batch_messages"] += len(messages)
        await asyncio.to_thread(handle_ticker_messages_batch, messages)
        return

    _WS_METRICS[f"handled_{msg_type or 'unknown'}"] += 1

    if msg_type == "ticker":
        if _WS_TICKER_BATCH_DB_WRITES_ENABLED:
            await asyncio.to_thread(handle_ticker_messages_batch, [data])
        else:
            await asyncio.to_thread(handle_ticker_message, data)
    elif msg_type == "trade":
        await asyncio.to_thread(handle_trade_message, data)
    elif msg_type == "orderbook_snapshot":
        await asyncio.to_thread(handle_orderbook_snapshot_message, data, session_id)
    elif msg_type == "orderbook_delta":
        await asyncio.to_thread(handle_orderbook_delta_message, data, session_id)
    elif msg_type == "error":
        logger.warning("WebSocket error payload: %s", data)
    else:
        logger.debug("Ignoring message type=%s", msg_type)


def _ws_metrics_metadata(
    *,
    session_id: str,
    queue_size: int,
    worker_count: int,
    session_message_count: int,
    session_started_at: float,
    pending_ticker_count: int,
) -> dict:
    now_m = time.monotonic()
    process_elapsed = max(0.001, now_m - _WS_METRICS_STARTED_AT)
    session_elapsed = max(0.001, now_m - session_started_at)
    message_types = {
        key.removeprefix("received_")
        for key in _WS_METRICS
        if key.startswith("received_")
    }
    message_types.update(
        key.removeprefix("queued_") for key in _WS_METRICS if key.startswith("queued_")
    )
    message_types.update(
        key.removeprefix("handled_")
        for key in _WS_METRICS
        if key.startswith("handled_")
    )

    metadata = {
        "session_id": session_id,
        "queue_size": queue_size,
        "worker_count": worker_count,
        "ticker_pending_count": pending_ticker_count,
        "messages_per_sec_process": round(
            sum(
                count
                for key, count in _WS_METRICS.items()
                if key.startswith("received_")
            )
            / process_elapsed,
            3,
        ),
        "messages_per_sec_session": round(session_message_count / session_elapsed, 3),
        "ticker_coalesce_enabled": _WS_TICKER_COALESCE_ENABLED,
        "ticker_coalesce_window_sec": _WS_TICKER_COALESCE_WINDOW_SEC,
        "ticker_flush_max_queue_fraction": _WS_TICKER_FLUSH_MAX_QUEUE_FRACTION,
        "ticker_batch_db_writes_enabled": _WS_TICKER_BATCH_DB_WRITES_ENABLED,
        "ticker_metrics_write_gate_enabled": _WS_TICKER_METRICS_WRITE_GATE_ENABLED,
        "ticker_metrics_min_interval_sec": _WS_TICKER_METRICS_MIN_INTERVAL_SEC,
        "ticker_known_market_gate_enabled": _WS_TICKER_KNOWN_MARKET_GATE_ENABLED,
        "ticker_snapshots_enabled": _WS_TICKER_SNAPSHOTS_ENABLED,
        "ticker_known_market_count": len(_KNOWN_MARKET_TICKERS),
        "ticker_known_market_age_sec": round(
            now_m - _KNOWN_MARKET_LAST_REFRESH,
            3,
        )
        if _KNOWN_MARKET_LAST_REFRESH
        else None,
    }
    for msg_type in sorted(message_types):
        metadata[f"received_{msg_type}"] = _WS_METRICS.get(f"received_{msg_type}", 0)
        metadata[f"queued_{msg_type}"] = _WS_METRICS.get(f"queued_{msg_type}", 0)
        metadata[f"handled_{msg_type}"] = _WS_METRICS.get(f"handled_{msg_type}", 0)
    for key in _WS_METRIC_KEYS:
        metadata[key] = _WS_METRICS.get(key, 0)
    return metadata


async def _ws_writer_worker(
    queue: asyncio.Queue[dict | None],
    session_id: str,
) -> None:
    while True:
        data = await queue.get()
        try:
            if data is None:
                return
            for attempt in range(2):
                try:
                    await _dispatch_ws_message(data, session_id)
                    break
                except OperationalError as exc:
                    if attempt == 0 and _is_deadlock_error(exc):
                        logger.warning(
                            "Retrying WS writer payload after Postgres deadlock"
                        )
                        await asyncio.sleep(0.05)
                        continue
                    raise
        except Exception:
            logger.exception("WebSocket writer worker failed for payload=%s", data)
        finally:
            queue.task_done()


def _is_deadlock_error(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None)
    return sqlstate == "40P01" or "deadlock detected" in str(exc).lower()


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
        db.query(
            MarketSnapshot.volume_24h_fp,
            MarketSnapshot.open_interest_fp,
        )
        .join(MarketMetric, MarketMetric.latest_snapshot_id == MarketSnapshot.id)
        .filter(MarketMetric.market_pk == market.id)
        .first()
    )

    # Fallback for markets whose metric row has not been initialized yet.
    if last is None:
        last = (
            db.query(
                MarketSnapshot.volume_24h_fp,
                MarketSnapshot.open_interest_fp,
            )
            .filter(MarketSnapshot.market_pk == market.id)
            .order_by(MarketSnapshot.id.desc())
            .first()
        )

    v24 = last.volume_24h_fp if last else None
    oi = last.open_interest_fp if last else None
    _tape_volume_cache[market.id] = (v24, oi, now_m)
    return v24, oi


def _raw_tape_decision(db, market: Market) -> StorageDecision:
    """Tiered decision for trades / book_event rows in this process."""
    v24, oi = _tape_hints_for_market(db, market)
    decision = storage_decision_for_event(
        market,
        volume_24h_fp=v24,
        open_interest_fp=oi,
        has_materialized_anomaly=False,
    )
    if decision.persist_raw_tape:
        return decision
    has_anomaly = (
        db.query(Anomaly.id).filter(Anomaly.market_pk == market.id).limit(1).first()
        is not None
    )
    if not has_anomaly:
        has_trade_flag = (
            db.query(TradeFlag.id)
            .filter(TradeFlag.market_pk == market.id)
            .filter(TradeFlag.severity.in_(("high", "critical")))
            .limit(1)
            .first()
            is not None
        )
        if not has_trade_flag:
            return decision
        return storage_decision_for_event(
            market,
            volume_24h_fp=v24,
            open_interest_fp=oi,
            signals=RetentionSignals(trade_burst_score=3.0),
        )
    return storage_decision_for_event(
        market,
        volume_24h_fp=v24,
        open_interest_fp=oi,
        has_materialized_anomaly=True,
    )


def _raw_tape_allowed(db, market: Market) -> bool:
    """Backward-compatible boolean raw-tape gate used by tests and callers."""
    return _raw_tape_decision(db, market).persist_raw_tape


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


def _parse_ticker_message(data: dict) -> dict | None:
    msg = data.get("msg") or {}
    market_ticker = msg.get("market_ticker")
    if not market_ticker:
        return None
    return {
        "data": data,
        "market_ticker": str(market_ticker),
        "last_price_dollars": parse_decimal(msg.get("last_price_dollars")),
        "yes_bid_dollars": parse_decimal(msg.get("yes_bid_dollars")),
        "yes_ask_dollars": parse_decimal(msg.get("yes_ask_dollars")),
        "no_bid_dollars": parse_decimal(msg.get("no_bid_dollars")),
        "no_ask_dollars": parse_decimal(msg.get("no_ask_dollars")),
        "volume_fp": parse_decimal(first_present(msg.get("volume_fp"), msg.get("volume"))),
        "volume_24h_fp": parse_decimal(
            first_present(
                msg.get("volume_24h_fp"),
                msg.get("volume_24h"),
                msg.get("volume24h"),
            )
        ),
        "open_interest_fp": parse_decimal(
            first_present(msg.get("open_interest_fp"), msg.get("open_interest"))
        ),
        "liquidity_dollars": parse_decimal(
            first_present(msg.get("liquidity_dollars"), msg.get("liquidity"))
        ),
    }


def _bulk_get_or_create_markets(
    db,
    market_tickers: list[str],
    *,
    create_missing: bool,
) -> dict[str, Market]:
    if not market_tickers:
        return {}

    unique_tickers = sorted(set(market_tickers))
    markets = (
        db.query(Market)
        .filter(Market.market_id.in_(unique_tickers))
        .all()
    )
    by_ticker = {str(m.market_id): m for m in markets}
    missing = [ticker for ticker in unique_tickers if ticker not in by_ticker]
    if not missing or not create_missing:
        if missing and not create_missing:
            _WS_METRICS["ticker_unknown_skipped"] += len(missing)
        return by_ticker

    rows = []
    for market_ticker in missing:
        in_scope, classification = is_ticker_in_scope(market_ticker)
        if not in_scope:
            continue
        rows.append(
            {
                "platform": "kalshi",
                "market_id": market_ticker,
                "title": market_ticker,
                "status": "unknown",
                "category": classification.category,
                "subcategory": classification.subcategory,
                "manipulability_prior": classification.manipulability_prior,
                "classifier_tags": list(classification.tags),
                "classifier_layer": classification.layer,
                "classifier_rule": classification.rule,
                "classifier_confidence": classification.confidence,
                "classifier_version": CLASSIFIER_VERSION,
            }
        )

    if rows:
        stmt = (
            pg_insert(Market)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["market_id"])
        )
        db.execute(stmt)
        db.flush()
        markets = (
            db.query(Market)
            .filter(Market.market_id.in_(unique_tickers))
            .all()
        )
        by_ticker = {str(m.market_id): m for m in markets}

    _WS_METRICS["ticker_unknown_skipped"] += max(0, len(missing) - len(rows))
    return by_ticker


def _latest_snapshot_rows_by_market(
    db,
    market_pks: list[int],
    *,
    include_snapshots: bool = True,
) -> tuple[dict[int, MarketMetric], dict[int, object]]:
    if not market_pks:
        return {}, {}

    metrics = (
        db.query(MarketMetric)
        .filter(MarketMetric.market_pk.in_(market_pks))
        .all()
    )
    metric_by_pk = {int(m.market_pk): m for m in metrics}
    if not include_snapshots:
        return metric_by_pk, {}

    snapshot_ids = [
        int(m.latest_snapshot_id)
        for m in metrics
        if m.latest_snapshot_id is not None
    ]
    snapshot_by_id = {}
    if snapshot_ids:
        snapshots = (
            db.query(
                MarketSnapshot.id,
                MarketSnapshot.market_pk,
                MarketSnapshot.ts,
                MarketSnapshot.last_price_dollars,
                MarketSnapshot.yes_bid_dollars,
                MarketSnapshot.yes_ask_dollars,
                MarketSnapshot.no_bid_dollars,
                MarketSnapshot.no_ask_dollars,
                MarketSnapshot.volume_fp,
                MarketSnapshot.volume_24h_fp,
                MarketSnapshot.open_interest_fp,
                MarketSnapshot.liquidity_dollars,
            )
            .filter(MarketSnapshot.id.in_(snapshot_ids))
            .all()
        )
        snapshot_by_id = {int(s.id): s for s in snapshots}

    latest_by_pk: dict[int, object] = {}
    for metric in metrics:
        if metric.latest_snapshot_id is None:
            continue
        snapshot = snapshot_by_id.get(int(metric.latest_snapshot_id))
        if snapshot is not None:
            latest_by_pk[int(metric.market_pk)] = snapshot

    missing_latest = [
        pk
        for pk in market_pks
        if pk not in latest_by_pk
    ]
    # Metrics should normally carry latest_snapshot_id. This fallback preserves
    # duplicate safety for older rows without putting the hot path back on the
    # expensive latest-snapshot query for every ticker.
    for pk in missing_latest[:25]:
        last = (
            db.query(MarketSnapshot)
            .filter(MarketSnapshot.market_pk == pk)
            .order_by(MarketSnapshot.id.desc())
            .first()
        )
        if last is not None:
            latest_by_pk[pk] = last

    return metric_by_pk, latest_by_pk


def _metric_updated_recently(metric: MarketMetric | None, now: datetime) -> bool:
    if metric is None or metric.updated_at is None:
        return False
    updated_at = metric.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (now - updated_at).total_seconds() < _WS_TICKER_METRICS_MIN_INTERVAL_SEC


def _should_upsert_ticker_metric(
    *,
    metric: MarketMetric | None,
    values: dict,
    has_new_snapshot: bool,
    now: datetime,
) -> bool:
    if not _WS_TICKER_METRICS_WRITE_GATE_ENABLED:
        return True
    if metric is None or has_new_snapshot:
        return True

    quote_fields = (
        "last_price_cents",
        "yes_bid_cents",
        "yes_ask_cents",
        "no_bid_cents",
        "no_ask_cents",
    )
    for field in quote_fields:
        if getattr(metric, field) != values.get(field):
            return True

    if _metric_updated_recently(metric, now):
        return False
    return True


def handle_ticker_messages_batch(messages: list[dict]) -> None:
    parsed = [p for message in messages if (p := _parse_ticker_message(message))]
    if not parsed:
        return

    # A coalesced batch should already have one ticker per market, but keep the
    # latest value here too so direct callers and non-coalesced single flushes
    # stay safe.
    latest_by_ticker: dict[str, dict] = {}
    for row in parsed:
        latest_by_ticker[row["market_ticker"]] = row
    parsed = list(latest_by_ticker.values())

    db = SessionLocal()
    try:
        markets_by_ticker = _bulk_get_or_create_markets(
            db,
            [row["market_ticker"] for row in parsed],
            create_missing=not _WS_TICKER_KNOWN_MARKET_GATE_ENABLED,
        )
        rows_with_markets: list[tuple[dict, Market]] = []
        for row in parsed:
            market = markets_by_ticker.get(row["market_ticker"])
            if market is None:
                logger.debug("Skipping unknown ticker message for %s", row["market_ticker"])
                continue
            rows_with_markets.append((row, market))

        if not rows_with_markets:
            db.rollback()
            return

        now = datetime.now(timezone.utc)
        market_pks = [int(market.id) for _row, market in rows_with_markets]
        metric_by_pk, latest_snapshot_by_pk = _latest_snapshot_rows_by_market(
            db,
            market_pks,
            include_snapshots=_WS_TICKER_SNAPSHOTS_ENABLED,
        )

        snapshots_to_add: list[tuple[MarketSnapshot, dict, Market, StorageDecision]] = []
        metric_rows: list[dict] = []
        chart_history_rows: list[dict] = []
        metric_heartbeat_decision: StorageDecision | None = None

        for row, market in rows_with_markets:
            _update_tape_hints_from_ticker(
                market.id,
                row["volume_24h_fp"],
                row["open_interest_fp"],
            )
            decision = storage_decision_for_event(
                market,
                volume_24h_fp=row["volume_24h_fp"],
                open_interest_fp=row["open_interest_fp"],
            )
            metric_heartbeat_decision = decision
            if should_write_quote_history(int(market.id), now):
                chart_row = quote_history_row(
                    market_pk=market.id,
                    event_ts=now,
                    last_price_dollars=row["last_price_dollars"],
                    yes_bid_dollars=row["yes_bid_dollars"],
                    yes_ask_dollars=row["yes_ask_dollars"],
                    volume_24h_fp=row["volume_24h_fp"],
                    open_interest_fp=row["open_interest_fp"],
                )
                if chart_row:
                    chart_history_rows.append(chart_row)
            should_store_snapshot = False
            if not _WS_TICKER_SNAPSHOTS_ENABLED:
                _WS_METRICS["ticker_snapshots_skipped_disabled"] += 1
            else:
                latest_snapshot = latest_snapshot_by_pk.get(int(market.id))
                is_duplicate = (
                    latest_snapshot is not None
                    and snapshot_row_is_duplicate(
                        latest_snapshot,
                        last_price_dollars=row["last_price_dollars"],
                        yes_bid_dollars=row["yes_bid_dollars"],
                        yes_ask_dollars=row["yes_ask_dollars"],
                        no_bid_dollars=row["no_bid_dollars"],
                        no_ask_dollars=row["no_ask_dollars"],
                        volume_fp=row["volume_fp"],
                        volume_24h_fp=row["volume_24h_fp"],
                        open_interest_fp=row["open_interest_fp"],
                        liquidity_dollars=row["liquidity_dollars"],
                        now=now,
                    )
                )
                if is_duplicate:
                    _WS_METRICS["ticker_snapshots_skipped_duplicate"] += 1
                else:
                    should_store_snapshot = _should_store_ticker_snapshot(
                        market,
                        (
                            f"ticker:{market.market_id}:{row['last_price_dollars']}:"
                            f"{row['yes_bid_dollars']}:{row['yes_ask_dollars']}:"
                            f"{row['volume_fp']}:{row['volume_24h_fp']}:"
                            f"{row['open_interest_fp']}"
                        ),
                        decision,
                    )
                    if not should_store_snapshot:
                        _WS_METRICS["ticker_snapshots_skipped_sampling"] += 1

            if should_store_snapshot:
                snapshot = MarketSnapshot(
                    market_pk=market.id,
                    last_price_dollars=row["last_price_dollars"],
                    yes_bid_dollars=row["yes_bid_dollars"],
                    yes_ask_dollars=row["yes_ask_dollars"],
                    no_bid_dollars=row["no_bid_dollars"],
                    no_ask_dollars=row["no_ask_dollars"],
                    volume_fp=row["volume_fp"],
                    volume_24h_fp=row["volume_24h_fp"],
                    open_interest_fp=row["open_interest_fp"],
                    liquidity_dollars=row["liquidity_dollars"],
                )
                db.add(snapshot)
                snapshots_to_add.append((snapshot, row, market, decision))
            else:
                values = quote_metric_values(
                    market_pk=market.id,
                    prior=market.manipulability_prior,
                    latest_snapshot_id=None,
                    latest_snapshot_ts=None,
                    last_price_dollars=row["last_price_dollars"],
                    yes_bid_dollars=row["yes_bid_dollars"],
                    yes_ask_dollars=row["yes_ask_dollars"],
                    no_bid_dollars=row["no_bid_dollars"],
                    no_ask_dollars=row["no_ask_dollars"],
                    volume_24h_fp=row["volume_24h_fp"],
                    open_interest_fp=row["open_interest_fp"],
                    liquidity_dollars=row["liquidity_dollars"],
                    decision=decision,
                )
                if _should_upsert_ticker_metric(
                    metric=metric_by_pk.get(int(market.id)),
                    values=values,
                    has_new_snapshot=False,
                    now=now,
                ):
                    metric_rows.append(values)
                else:
                    _WS_METRICS["ticker_metrics_skipped_gate"] += 1

        if snapshots_to_add:
            db.flush()
            _WS_METRICS["ticker_snapshots_inserted"] += len(snapshots_to_add)
            for snapshot, row, market, decision in snapshots_to_add:
                metric_rows.append(
                    quote_metric_values(
                        market_pk=market.id,
                        prior=market.manipulability_prior,
                        latest_snapshot_id=snapshot.id,
                        latest_snapshot_ts=snapshot.ts,
                        last_price_dollars=row["last_price_dollars"],
                        yes_bid_dollars=row["yes_bid_dollars"],
                        yes_ask_dollars=row["yes_ask_dollars"],
                        no_bid_dollars=row["no_bid_dollars"],
                        no_ask_dollars=row["no_ask_dollars"],
                        volume_24h_fp=row["volume_24h_fp"],
                        open_interest_fp=row["open_interest_fp"],
                        liquidity_dollars=row["liquidity_dollars"],
                        decision=decision,
                    )
                )
                if _write_raw_clickhouse():
                    clickhouse_batcher.enqueue(
                        _CH_QUOTES_TABLE,
                        {
                            "ts": _clickhouse_ts(snapshot.ts),
                            "market_pk": market.id,
                            "last_price_cents": _cents(row["last_price_dollars"]),
                            "yes_bid_cents": _cents(row["yes_bid_dollars"]),
                            "yes_ask_cents": _cents(row["yes_ask_dollars"]),
                            "no_bid_cents": _cents(row["no_bid_dollars"]),
                            "no_ask_cents": _cents(row["no_ask_dollars"]),
                            "volume_24h_contracts": _contracts(row["volume_24h_fp"]),
                            "open_interest_contracts": _contracts(row["open_interest_fp"]),
                            "storage_tier": decision.tier,
                        },
                    )
                if _should_materialize_market_anomaly(market.id, decision):
                    materialize_market_anomaly(
                        db,
                        market,
                        lookback=40,
                        latest_snapshot_id=snapshot.id,
                    )

        if metric_rows:
            bulk_upsert_quote_metrics(
                db,
                metric_rows,
                heartbeat_decision=metric_heartbeat_decision,
            )
            _WS_METRICS["ticker_metrics_upserted"] += len(metric_rows)
        if chart_history_rows:
            bulk_upsert_chart_history(db, chart_history_rows)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def handle_ticker_message(data: dict) -> None:
    if _WS_TICKER_BATCH_DB_WRITES_ENABLED:
        handle_ticker_messages_batch([data])
        return

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
        vol = parse_decimal(first_present(msg.get("volume_fp"), msg.get("volume")))
        v24 = parse_decimal(
            first_present(
                msg.get("volume_24h_fp"),
                msg.get("volume_24h"),
                msg.get("volume24h"),
            )
        )
        oi = parse_decimal(first_present(msg.get("open_interest_fp"), msg.get("open_interest")))
        liq = parse_decimal(first_present(msg.get("liquidity_dollars"), msg.get("liquidity")))
        _update_tape_hints_from_ticker(market.id, v24, oi)
        decision = storage_decision_for_event(
            market,
            volume_24h_fp=v24,
            open_interest_fp=oi,
        )
        now = datetime.now(timezone.utc)
        if should_write_quote_history(int(market.id), now):
            upsert_chart_history(
                db,
                quote_history_row(
                    market_pk=market.id,
                    event_ts=now,
                    last_price_dollars=lp,
                    yes_bid_dollars=yb,
                    yes_ask_dollars=ya,
                    volume_24h_fp=v24,
                    open_interest_fp=oi,
                ),
            )
        if not _WS_TICKER_SNAPSHOTS_ENABLED:
            _WS_METRICS["ticker_snapshots_skipped_disabled"] += 1
            upsert_quote_metrics(
                db,
                market_pk=market.id,
                prior=market.manipulability_prior,
                latest_snapshot_id=None,
                latest_snapshot_ts=None,
                last_price_dollars=lp,
                yes_bid_dollars=yb,
                yes_ask_dollars=ya,
                no_bid_dollars=nb,
                no_ask_dollars=na,
                volume_24h_fp=v24,
                open_interest_fp=oi,
                liquidity_dollars=liq,
                decision=decision,
            )
            db.commit()
            return

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
            upsert_quote_metrics(
                db,
                market_pk=market.id,
                prior=market.manipulability_prior,
                latest_snapshot_id=None,
                latest_snapshot_ts=None,
                last_price_dollars=lp,
                yes_bid_dollars=yb,
                yes_ask_dollars=ya,
                no_bid_dollars=nb,
                no_ask_dollars=na,
                volume_24h_fp=v24,
                open_interest_fp=oi,
                liquidity_dollars=liq,
                decision=decision,
            )
            db.commit()
            return

        should_store_snapshot = _should_store_ticker_snapshot(
            market,
            f"ticker:{market.market_id}:{lp}:{yb}:{ya}:{vol}:{v24}:{oi}",
            decision,
        )
        if not should_store_snapshot:
            _WS_METRICS["ticker_snapshots_skipped_sampling"] += 1
            upsert_quote_metrics(
                db,
                market_pk=market.id,
                prior=market.manipulability_prior,
                latest_snapshot_id=None,
                latest_snapshot_ts=None,
                last_price_dollars=lp,
                yes_bid_dollars=yb,
                yes_ask_dollars=ya,
                no_bid_dollars=nb,
                no_ask_dollars=na,
                volume_24h_fp=v24,
                open_interest_fp=oi,
                liquidity_dollars=liq,
                decision=decision,
            )
            db.commit()
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
        upsert_quote_metrics(
            db,
            market_pk=market.id,
            prior=market.manipulability_prior,
            latest_snapshot_id=snapshot.id,
            latest_snapshot_ts=snapshot.ts,
            last_price_dollars=lp,
            yes_bid_dollars=yb,
            yes_ask_dollars=ya,
            no_bid_dollars=nb,
            no_ask_dollars=na,
            volume_24h_fp=v24,
            open_interest_fp=oi,
            liquidity_dollars=liq,
            decision=decision,
        )
        if _write_raw_clickhouse():
            clickhouse_batcher.enqueue(
                _CH_QUOTES_TABLE,
                {
                    "ts": _clickhouse_ts(snapshot.ts),
                    "market_pk": market.id,
                    "last_price_cents": _cents(lp),
                    "yes_bid_cents": _cents(yb),
                    "yes_ask_cents": _cents(ya),
                    "no_bid_cents": _cents(nb),
                    "no_ask_cents": _cents(na),
                    "volume_24h_contracts": _contracts(v24),
                    "open_interest_contracts": _contracts(oi),
                    "storage_tier": decision.tier,
                },
            )

        if _should_materialize_market_anomaly(market.id, decision):
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

        decision = _raw_tape_decision(db, market)
        if not decision.persist_raw_tape:
            # Lazy-upsert may have just inserted a stub `Market`; commit so it
            # survives even when we drop the trade on the floor (raw-tape gate).
            db.commit()
            return
        if not should_sample_event(
            f"trade:{market.market_id}:{trade_id}", decision.sample_rate
        ):
            db.commit()
            return

        ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

        yes_price = parse_decimal(msg.get("yes_price_dollars"))
        no_price = parse_decimal(msg.get("no_price_dollars"))
        count = parse_decimal(msg.get("count_fp"))
        wrote_trade = False
        if _write_raw_postgres():
            # ON CONFLICT DO NOTHING keeps reconnect-replays cheap and avoids
            # poisoning the transaction with IntegrityErrors per duplicate.
            stmt = (
                pg_insert(Trade)
                .values(
                    market_pk=market.id,
                    trade_id=trade_id,
                    ts=ts,
                    yes_price_dollars=yes_price,
                    no_price_dollars=no_price,
                    count_fp=count,
                    taker_side=msg.get("taker_side"),
                )
                .on_conflict_do_nothing(index_elements=["trade_id"])
            )
            # SQLAlchemy 2's static return type is `Result[Any]`, but a DML
            # statement executes as a `CursorResult` at runtime, which is what
            # exposes `rowcount`. Suppress the false-positive attribute error.
            result = db.execute(stmt)
            wrote_trade = bool(result.rowcount)  # type: ignore[attr-defined]

        if _write_raw_clickhouse() and (wrote_trade or not _write_raw_postgres()):
            wrote_trade = (
                clickhouse_batcher.enqueue(
                    _CH_TRADES_TABLE,
                    {
                        "ts": ts,
                        "market_pk": market.id,
                        "trade_id": trade_id,
                        "yes_price_cents": _cents(yes_price),
                        "no_price_cents": _cents(no_price),
                        "count_contracts": _contracts(count),
                        "taker_side": _side_enum(msg.get("taker_side")),
                        "storage_tier": decision.tier,
                        "anomaly_context": 1 if decision.store_case_evidence else 0,
                    },
                )
                or wrote_trade
            )

        if wrote_trade:
            upsert_chart_history(
                db,
                trade_history_row(
                    market_pk=market.id,
                    trade_ts=ts,
                    yes_price_dollars=yes_price,
                    count_fp=count,
                ),
            )
            bump_trade_metrics(db, market_pk=market.id, trade_ts=ts)
        db.commit()

        if wrote_trade:
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
        market = (
            db.query(Market).filter(Market.market_id == market_ticker).one_or_none()
        )
        if market is None:
            logger.warning(
                "Skipping orderbook_snapshot for unknown market_ticker=%s",
                market_ticker,
            )
            return

        if not _raw_tape_allowed(db, market):
            return
        decision: StorageDecision | None = None
        sample_rate = 1.0
        try:
            decision = _raw_tape_decision(db, market)
            sample_rate = decision.sample_rate
        except AttributeError:
            # Unit-test fakes patch the boolean gate and intentionally omit
            # enough query methods for the richer decision lookup.
            pass
        if not should_sample_event(
            f"book_snapshot:{market.market_id}:{session_id}:{seq}",
            sample_rate,
        ):
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

        if _write_raw_clickhouse():
            for row in rows:
                clickhouse_batcher.enqueue(
                    _CH_L2_TABLE,
                    {
                        "ts": _clickhouse_ts(row["ts"]),
                        "market_pk": row["market_pk"],
                        "session_id": row["session_id"],
                        "seq": row["seq"],
                        "side": row["side"],
                        "price_cents": _cents(row["price_dollars"]),
                        "size_contracts": _contracts(row["size_fp"]),
                        "delta_contracts": None,
                        "is_snapshot": 1,
                        "storage_tier": decision.tier if decision else "hot",
                    },
                )

        if _write_raw_postgres():
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
    if side not in {"yes", "no"}:
        logger.warning("Skipping orderbook_delta with unknown side: %s", data)
        return

    db = SessionLocal()
    try:
        market = (
            db.query(Market).filter(Market.market_id == market_ticker).one_or_none()
        )
        if market is None:
            logger.warning(
                "Skipping orderbook_delta for unknown market_ticker=%s",
                market_ticker,
            )
            return

        if not _raw_tape_allowed(db, market):
            return
        decision: StorageDecision | None = None
        sample_rate = 1.0
        try:
            decision = _raw_tape_decision(db, market)
            sample_rate = decision.sample_rate
        except AttributeError:
            pass
        if not should_sample_event(
            f"book_delta:{market.market_id}:{session_id}:{seq}:{side}:{price_str}",
            sample_rate,
        ):
            return

        ts = _ts_from_ms(msg.get("ts_ms"))
        price = parse_decimal(price_str)
        delta = parse_decimal(delta_str)

        if _write_raw_clickhouse():
            clickhouse_batcher.enqueue(
                _CH_L2_TABLE,
                {
                    "ts": _clickhouse_ts(ts),
                    "market_pk": market.id,
                    "session_id": session_id,
                    "seq": seq,
                    "side": side,
                    "price_cents": _cents(price),
                    "size_contracts": None,
                    "delta_contracts": _signed_contracts(delta),
                    "is_snapshot": 0,
                    "storage_tier": decision.tier if decision else "hot",
                },
            )

        if not _write_raw_postgres():
            return

        stmt = (
            pg_insert(BookEvent)
            .values(
                market_pk=market.id,
                session_id=session_id,
                seq=seq,
                ts=ts,
                side=side,
                price_dollars=price,
                size_fp=None,
                delta_fp=delta,
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
      2. The most actively-trading markets from the compact `market_metrics`
         projection. This avoids scanning the multi-million-row snapshot table
         during websocket startup.
      3. Cold-start fallback: most-recently-updated markets, used only when
         no snapshots with positive volume exist yet (e.g. brand-new DB).

    Smoke-test war story: the previous implementation was just (1) -> (3).
    On a fresh bootstrap of 50k markets, Kalshi's alphabetical pagination
    front-loaded ~50k dead exotic combinatorial markets, so step (3) picked
    50 of those and we got zero `orderbook_snapshot` events for a full
    minute against live Kalshi. Volume-ranking fixes that even when the
    bootstrap is biased.

    We treat 'active', 'open', and 'unknown' as tradeable since 'unknown' is
    the sentinel set by the lazy-upsert path for markets we've only seen via
    the WS feed.
    """
    if settings.kalshi_book_market_tickers:
        return list(settings.kalshi_book_market_tickers)

    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(minutes=_BOOK_MARKET_ACTIVITY_RECENCY_MINUTES)
    live_ranked_market = or_(
        MarketMetric.latest_snapshot_ts >= recent_cutoff,
        MarketMetric.last_trade_ts >= recent_cutoff,
        MarketMetric.volume_24h_contracts > 0,
        MarketMetric.trade_count > 0,
    )
    live_fallback_market = or_(
        Market.close_time > now,
        and_(Market.close_time.is_(None), Market.updated_at >= recent_cutoff),
    )

    db = SessionLocal()
    try:
        ranked = db.execute(
            select(Market.market_id)
            .join(MarketMetric, MarketMetric.market_pk == Market.id)
            .where(Market.status.in_(["active", "open", "unknown"]))
            .where(live_ranked_market)
            .where(
                (MarketMetric.volume_24h_contracts > 0)
                | (MarketMetric.trade_count > 0)
                | (MarketMetric.latest_snapshot_ts.is_not(None))
            )
            .order_by(
                MarketMetric.volume_24h_contracts.desc().nullslast(),
                MarketMetric.trade_count.desc(),
                MarketMetric.latest_snapshot_ts.desc().nullslast(),
                MarketMetric.updated_at.desc(),
            )
            .limit(settings.kalshi_book_market_limit)
        ).all()

        if ranked:
            return [r[0] for r in ranked]

        rows = (
            db.query(Market.market_id)
            .filter(Market.status.in_(["active", "open"]))
            .filter(live_fallback_market)
            .order_by(Market.updated_at.desc())
            .limit(settings.kalshi_book_market_limit)
            .all()
        )
        return [r[0] for r in rows]
    finally:
        db.close()


async def consume_market_data_forever() -> None:
    backoff_seconds = 1
    run_id = new_run_id("ws")
    mark_pipeline_start(
        "ws_trade_feed",
        detail="WebSocket consumer starting.",
        run_id=run_id,
    )

    while True:
        try:
            session_id = str(uuid.uuid4())
            headers = create_ws_headers(
                settings.kalshi_api_key_id,
                settings.kalshi_private_key_path,
                settings.kalshi_private_key_pem,
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
                record_pipeline_heartbeat(
                    "ws_trade_feed",
                    detail="Connected and subscribed to ticker + trade.",
                    run_id=run_id,
                    metadata={"session_id": session_id},
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
                    logger.info(
                        "Subscribed to orderbook_delta for %s markets.",
                        len(book_tickers),
                    )
                else:
                    logger.info(
                        "No markets resolved for orderbook_delta; skipping subscription."
                    )

                backoff_seconds = 1
                queue: asyncio.Queue[dict | None] = asyncio.Queue(
                    maxsize=settings.kalshi_ws_queue_size
                )
                workers = [
                    asyncio.create_task(_ws_writer_worker(queue, session_id))
                    for _ in range(max(1, settings.kalshi_ws_worker_count))
                ]
                pending_tickers: dict[str, dict] = {}
                pending_lock = asyncio.Lock()
                coalescer_stop = asyncio.Event()
                coalescer_task = (
                    asyncio.create_task(
                        _ticker_coalescer_worker(
                            pending_tickers=pending_tickers,
                            pending_lock=pending_lock,
                            queue=queue,
                            stop_event=coalescer_stop,
                        )
                    )
                    if _WS_TICKER_COALESCE_ENABLED
                    else None
                )
                known_market_stop = asyncio.Event()
                known_market_task = (
                    asyncio.create_task(_known_market_ticker_worker(known_market_stop))
                    if _WS_TICKER_KNOWN_MARKET_GATE_ENABLED
                    else None
                )

                try:
                    last_heartbeat = time.monotonic()
                    session_started_at = last_heartbeat
                    message_count = 0
                    async for raw_message in websocket:
                        data = json.loads(raw_message)
                        msg_type = _ws_msg_type(data)
                        _WS_METRICS[f"received_{msg_type}"] += 1
                        message_count += 1

                        ticker_key = _ticker_market_key(data)
                        if ticker_key and not _ticker_allowed_by_known_market_gate(ticker_key):
                            _WS_METRICS["ticker_known_market_gate_dropped"] += 1
                            continue

                        if _WS_TICKER_COALESCE_ENABLED and ticker_key:
                            # Ticker updates are market state snapshots where latest
                            # state wins. Trades and orderbook streams are event
                            # streams and must keep their original ordering/granularity.
                            async with pending_lock:
                                if ticker_key in pending_tickers:
                                    _WS_METRICS["ticker_coalesced"] += 1
                                else:
                                    _WS_METRICS["ticker_pending_new"] += 1
                                pending_tickers[ticker_key] = data
                        else:
                            await queue.put(data)
                            _WS_METRICS[f"queued_{msg_type}"] += 1

                        now_m = time.monotonic()
                        if now_m - last_heartbeat >= _WS_METRICS_LOG_INTERVAL_SEC:
                            async with pending_lock:
                                pending_count = len(pending_tickers)
                            metadata = _ws_metrics_metadata(
                                session_id=session_id,
                                queue_size=queue.qsize(),
                                worker_count=len(workers),
                                session_message_count=message_count,
                                session_started_at=session_started_at,
                                pending_ticker_count=pending_count,
                            )
                            record_pipeline_heartbeat(
                                "ws_trade_feed",
                                detail=(
                                    f"Connected; received {message_count} messages "
                                    f"this session."
                                ),
                                run_id=run_id,
                                count=message_count,
                                metadata=metadata,
                            )
                            logger.info("WebSocket metrics: %s", metadata)
                            last_heartbeat = now_m
                finally:
                    if known_market_task is not None:
                        known_market_stop.set()
                        await known_market_task
                    if coalescer_task is not None:
                        coalescer_stop.set()
                        await coalescer_task
                    for _ in workers:
                        await queue.put(None)
                    await queue.join()
                    await asyncio.gather(*workers, return_exceptions=True)

        except Exception as exc:
            mark_pipeline_error(
                "ws_trade_feed",
                exc,
                detail=f"WebSocket error; reconnecting in {backoff_seconds}s.",
                run_id=run_id,
            )
            logger.warning(
                "WebSocket consumer error: %s. Reconnecting in %ss...",
                exc,
                backoff_seconds,
            )
            await asyncio.sleep(backoff_seconds)
            backoff_seconds = min(backoff_seconds * 2, 30)
