"""Compact old 5-minute chart history into 1-hour buckets.

Default mode is a dry run. No rows are written unless ``--execute`` is passed,
and 5-minute source rows are not removed unless both ``--execute`` and
``--replace-source-rows`` are passed.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from app.db.models import Market, MarketPriceHistory
from app.db.session import SessionLocal
from app.services.market_lifecycle import (
    ACTIVE_MARKET_STATUSES,
    scheduled_event_date_from_market_id,
)
from app.services.market_price_history import (
    DEFAULT_CHART_HISTORY_INTERVAL_SEC,
    bucket_start,
    bulk_upsert_chart_history,
)
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
)


TARGET_INTERVAL_SEC = 3600
CHART_HISTORY_COMPACTION_LOCK_KEY = "predictionmarkets:chart_history_compaction"
logger = logging.getLogger(__name__)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _closed_or_resolved_before(market: Market, cutoff: datetime) -> bool:
    close_time = _as_utc(market.close_time)
    if close_time is not None and close_time <= cutoff:
        return True

    status = (market.status or "").lower()
    if status and status not in ACTIVE_MARKET_STATUSES and status not in {
        "unknown",
        "out_of_scope",
    }:
        updated_at = _as_utc(market.updated_at)
        return updated_at is None or updated_at <= cutoff

    # Kalshi can leave event markets locally active until a per-market refresh
    # corrects the row. For compaction, a dated ticker older than the stale
    # grace is equivalent to resolved for chart granularity purposes.
    event_date = scheduled_event_date_from_market_id(market.market_id)
    return event_date is not None and event_date + timedelta(hours=30) <= cutoff


def _candidate_markets(
    db: Session,
    *,
    cutoff: datetime,
    source_interval_sec: int,
    max_markets: int,
    offset: int,
) -> list[Market]:
    rows = (
        db.query(Market)
        .join(MarketPriceHistory, MarketPriceHistory.market_pk == Market.id)
        .filter(MarketPriceHistory.interval_sec == source_interval_sec)
        .filter(MarketPriceHistory.bucket_start <= cutoff)
        .group_by(Market.id)
        .order_by(Market.id.asc())
        .offset(max(0, offset))
        .limit(max(1, max_markets) * 10)
        .all()
    )
    out: list[Market] = []
    for market in rows:
        if _closed_or_resolved_before(market, cutoff):
            out.append(market)
            if len(out) >= max(1, max_markets):
                break
    return out


def _source_rows(
    db: Session,
    market_pk: int,
    *,
    cutoff: datetime,
    source_interval_sec: int,
    max_rows: int,
) -> list[MarketPriceHistory]:
    q = (
        db.query(MarketPriceHistory)
        .filter(MarketPriceHistory.market_pk == market_pk)
        .filter(MarketPriceHistory.interval_sec == source_interval_sec)
        .filter(MarketPriceHistory.bucket_start <= cutoff)
        .order_by(MarketPriceHistory.bucket_start.asc())
    )
    if max_rows > 0:
        q = q.limit(max_rows)
    return q.all()


def _target_row_from_source(
    source: MarketPriceHistory,
    *,
    target_interval_sec: int,
) -> dict[str, Any]:
    return {
        "market_pk": int(source.market_pk),
        "interval_sec": int(target_interval_sec),
        "bucket_start": bucket_start(
            source.bucket_start,
            interval_sec=target_interval_sec,
        ),
        "open_price_dollars": source.open_price_dollars,
        "high_price_dollars": source.high_price_dollars,
        "low_price_dollars": source.low_price_dollars,
        "close_price_dollars": source.close_price_dollars,
        "close_price_source": source.close_price_source,
        "close_price_source_rank": source.close_price_source_rank,
        "first_price_ts": source.first_price_ts,
        "last_price_ts": source.last_price_ts,
        "open_yes_bid_dollars": source.open_yes_bid_dollars,
        "high_yes_bid_dollars": source.high_yes_bid_dollars,
        "low_yes_bid_dollars": source.low_yes_bid_dollars,
        "close_yes_bid_dollars": source.close_yes_bid_dollars,
        "open_yes_ask_dollars": source.open_yes_ask_dollars,
        "high_yes_ask_dollars": source.high_yes_ask_dollars,
        "low_yes_ask_dollars": source.low_yes_ask_dollars,
        "close_yes_ask_dollars": source.close_yes_ask_dollars,
        "close_volume_24h_fp": source.close_volume_24h_fp,
        "close_open_interest_fp": source.close_open_interest_fp,
        "first_quote_ts": source.first_quote_ts,
        "last_quote_ts": source.last_quote_ts,
        "trade_count": int(source.trade_count or 0),
        "trade_volume_contracts": source.trade_volume_contracts,
        "quote_count": int(source.quote_count or 0),
    }


def _delete_source_rows(
    db: Session,
    market_pk: int,
    bucket_starts: list[datetime],
    *,
    source_interval_sec: int,
) -> int:
    if not bucket_starts:
        return 0
    result = db.execute(
        delete(MarketPriceHistory)
        .where(MarketPriceHistory.market_pk == market_pk)
        .where(MarketPriceHistory.interval_sec == source_interval_sec)
        .where(MarketPriceHistory.bucket_start.in_(bucket_starts))
    )
    return int(result.rowcount or 0)


def _try_chart_history_compaction_lock(db: Session) -> bool:
    return bool(
        db.execute(
            text("select pg_try_advisory_lock(hashtext(:key))"),
            {"key": CHART_HISTORY_COMPACTION_LOCK_KEY},
        ).scalar()
    )


def _release_chart_history_compaction_lock(db: Session) -> None:
    db.execute(
        text("select pg_advisory_unlock(hashtext(:key))"),
        {"key": CHART_HISTORY_COMPACTION_LOCK_KEY},
    )


def compact_chart_history(
    db: Session,
    *,
    grace_days_after_close: int,
    source_interval_sec: int,
    target_interval_sec: int,
    max_markets: int,
    offset: int,
    max_source_rows_per_market: int,
    execute: bool,
    replace_source_rows: bool,
) -> dict[str, Any]:
    if execute and not replace_source_rows:
        raise ValueError(
            "Unsafe chart-history compaction: --execute must be paired with "
            "--replace-source-rows. bulk_upsert_chart_history adds aggregate "
            "counts on conflict, so executing without deleting compacted source "
            "rows can double-count on the next run."
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days_after_close)
    markets = _candidate_markets(
        db,
        cutoff=cutoff,
        source_interval_sec=source_interval_sec,
        max_markets=max_markets,
        offset=offset,
    )
    result: dict[str, Any] = {
        "dry_run": not execute,
        "replace_source_rows": bool(replace_source_rows and execute),
        "grace_days_after_close": grace_days_after_close,
        "source_interval_sec": source_interval_sec,
        "target_interval_sec": target_interval_sec,
        "cutoff": cutoff.isoformat(),
        "markets": len(markets),
        "source_rows": 0,
        "target_buckets": 0,
        "target_rows_upserted": 0,
        "source_rows_deleted": 0,
    }

    for market in markets:
        source_rows = _source_rows(
            db,
            int(market.id),
            cutoff=cutoff,
            source_interval_sec=source_interval_sec,
            max_rows=max_source_rows_per_market,
        )
        target_rows = [
            _target_row_from_source(row, target_interval_sec=target_interval_sec)
            for row in source_rows
        ]
        target_bucket_count = len(
            {
                (
                    int(row["market_pk"]),
                    int(row["interval_sec"]),
                    row["bucket_start"],
                )
                for row in target_rows
            }
        )

        result["source_rows"] += len(source_rows)
        result["target_buckets"] += target_bucket_count

        if execute and target_rows:
            bulk_upsert_chart_history(db, target_rows)
            result["target_rows_upserted"] += target_bucket_count
            if replace_source_rows:
                result["source_rows_deleted"] += _delete_source_rows(
                    db,
                    int(market.id),
                    [row.bucket_start for row in source_rows],
                    source_interval_sec=source_interval_sec,
                )
            db.commit()

    if not execute:
        db.rollback()
    return result

def _compaction_policies_from_args(args: argparse.Namespace) -> list[dict[str, int]]:
    raw = str(getattr(args, "policies", "") or "").strip()
    if not raw:
        return [
            {
                "source_interval_sec": max(1, args.source_interval_sec),
                "target_interval_sec": max(1, args.target_interval_sec),
                "grace_days_after_close": max(0, args.grace_days_after_close),
            }
        ]

    policies: list[dict[str, int]] = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        fields = [field.strip() for field in value.split(":")]
        if len(fields) != 3:
            raise ValueError(
                "chart compaction policies must be source:target:grace_days entries"
            )
        source, target, grace = (int(field) for field in fields)
        policies.append(
            {
                "source_interval_sec": max(1, source),
                "target_interval_sec": max(1, target),
                "grace_days_after_close": max(0, grace),
            }
        )
    if not policies:
        raise ValueError("at least one chart compaction policy is required")
    return policies


def _run_once(args: argparse.Namespace) -> dict[str, Any]:
    db = SessionLocal()
    locked = False
    try:
        locked = _try_chart_history_compaction_lock(db)
        if not locked:
            logger.info("chart-history compaction lock skipped")
            return {
                "dry_run": not args.execute,
                "replace_source_rows": False,
                "skipped": True,
                "reason": "another_chart_history_compaction_running",
                "lock_acquired": False,
                "grace_days_after_close": args.grace_days_after_close,
                "source_interval_sec": args.source_interval_sec,
                "target_interval_sec": args.target_interval_sec,
                "markets": 0,
                "source_rows": 0,
                "target_buckets": 0,
                "target_rows_upserted": 0,
                "source_rows_deleted": 0,
            }

        logger.info("chart-history compaction lock acquired")
        policies = _compaction_policies_from_args(args)
        policy_results = []
        for policy in policies:
            policy_results.append(
                compact_chart_history(
                    db,
                    grace_days_after_close=policy["grace_days_after_close"],
                    source_interval_sec=policy["source_interval_sec"],
                    target_interval_sec=policy["target_interval_sec"],
                    max_markets=max(1, args.max_markets),
                    offset=max(0, args.offset),
                    max_source_rows_per_market=max(0, args.max_source_rows_per_market),
                    execute=bool(args.execute),
                    replace_source_rows=bool(args.replace_source_rows),
                )
            )
        first_policy = policies[0]
        result = {
            "dry_run": not args.execute,
            "replace_source_rows": bool(args.replace_source_rows and args.execute),
            "grace_days_after_close": first_policy["grace_days_after_close"],
            "source_interval_sec": first_policy["source_interval_sec"],
            "target_interval_sec": first_policy["target_interval_sec"],
            "policies": policy_results,
            "markets": sum(int(item.get("markets") or 0) for item in policy_results),
            "source_rows": sum(
                int(item.get("source_rows") or 0) for item in policy_results
            ),
            "target_buckets": sum(
                int(item.get("target_buckets") or 0) for item in policy_results
            ),
            "target_rows_upserted": sum(
                int(item.get("target_rows_upserted") or 0) for item in policy_results
            ),
            "source_rows_deleted": sum(
                int(item.get("source_rows_deleted") or 0) for item in policy_results
            ),
            "lock_acquired": True,
        }
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        if locked:
            _release_chart_history_compaction_lock(db)
            logger.info("chart-history compaction lock released")
        db.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--replace-source-rows", action="store_true")
    parser.add_argument("--grace-days-after-close", type=int, default=7)
    parser.add_argument(
        "--source-interval-sec",
        type=int,
        default=DEFAULT_CHART_HISTORY_INTERVAL_SEC,
    )
    parser.add_argument("--target-interval-sec", type=int, default=TARGET_INTERVAL_SEC)
    parser.add_argument(
        "--policies",
        default="",
        help=(
            "Optional comma-separated source:target:grace_days policies, e.g. "
            "300:3600:7,3600:86400:180."
        ),
    )
    parser.add_argument("--max-markets", type=int, default=100)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-source-rows-per-market", type=int, default=0)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=21_600.0)
    args = parser.parse_args()

    while True:
        run_id = new_run_id("chart-history-compaction")
        mark_pipeline_start(
            "chart_history_compaction",
            detail="Starting chart-history compaction.",
            run_id=run_id,
            metadata={
                "dry_run": not args.execute,
                "replace_source_rows": bool(args.replace_source_rows and args.execute),
                "watch": bool(args.watch),
            },
        )
        try:
            result = _run_once(args)
            if result.get("skipped"):
                detail = (
                    "Skipped chart-history compaction; another sweep is already "
                    "running."
                )
                count = 0
            else:
                detail = (
                    f"Chart-history compaction examined {result['source_rows']} "
                    f"source rows into {result['target_buckets']} target buckets."
                )
                count = int(result["target_buckets"])
            mark_pipeline_success(
                "chart_history_compaction",
                detail=detail,
                run_id=run_id,
                count=count,
                metadata=result,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
        except Exception as exc:
            logger.exception("chart-history compaction failed")
            mark_pipeline_error(
                "chart_history_compaction",
                exc,
                detail="chart-history compaction failed.",
                run_id=run_id,
            )
            if not args.watch:
                raise

        if not args.watch:
            return
        time.sleep(max(60.0, args.interval_seconds))


if __name__ == "__main__":
    main()
