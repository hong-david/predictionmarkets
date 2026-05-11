"""Run bounded retention cleanup for bulky Postgres hot tables.

Dry-run is the default. Pass ``--execute`` to delete rows. The script works in
small batches so it can be scheduled frequently without holding long table
locks. It preserves the latest snapshot per market and snapshots referenced by
stored anomalies.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import BookEvent
from app.db.session import SessionLocal
from app.services.market_price_history import DEFAULT_CHART_HISTORY_INTERVAL_SEC
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
)

logger = logging.getLogger(__name__)
DEFAULT_COMPACT_CHART_HISTORY_INTERVAL_SEC = int(
    os.getenv("KALSHI_CHART_HISTORY_COMPACT_INTERVAL_SEC", "3600")
)
DEFAULT_RETENTION_MAX_BATCHES = int(os.getenv("KALSHI_RETENTION_MAX_BATCHES", "20"))
TRADE_RETENTION_DAYS_BY_TIER = {
    "observe_only": 1,
    "sampled": 14,
    "hot": 30,
    "triggered": 90,
    "case": 365,
}
_CLOSED_MARKET_STATUS_SQL = """
(
  (
    m.close_time IS NOT NULL
    AND m.close_time < :close_cutoff
  )
  OR (
    m.close_time IS NULL
    AND t.ts < :close_cutoff
    AND lower(coalesce(m.status, '')) IN (
      'closed',
      'settled',
      'resolved',
      'finalized',
      'expired'
    )
  )
)
"""


def _cutoff(days: int) -> datetime:
    # Python datetimes cannot subtract arbitrary operator input forever; clamp
    # to a conservative "effectively never" value for dry-run smoke checks.
    return datetime.now(timezone.utc) - timedelta(days=min(max(0, days), 100_000))


def _count_book_events(db: Session, cutoff: datetime) -> int:
    return int(
        db.execute(
            select(func.count()).select_from(BookEvent).where(BookEvent.received_at < cutoff)
        ).scalar_one()
        or 0
    )


def _delete_book_events_batch(db: Session, cutoff: datetime, *, batch_size: int) -> int:
    ids = (
        select(BookEvent.id)
        .where(BookEvent.received_at < cutoff)
        .order_by(BookEvent.id.asc())
        .limit(batch_size)
    )
    result = db.execute(delete(BookEvent).where(BookEvent.id.in_(ids)))
    return int(result.rowcount or 0)


def _count_snapshots(
    db: Session,
    tier: str,
    cutoff: datetime,
    *,
    require_chart_history: bool,
    live_chart_interval_sec: int,
    compact_chart_interval_sec: int,
) -> int:
    return int(
        db.execute(
            text(
                """
                SELECT count(*)::bigint
                FROM market_snapshots ms
                JOIN market_metrics mm ON mm.market_pk = ms.market_pk
                WHERE mm.storage_tier = :tier
                  AND ms.ts < :cutoff
                  AND (
                    mm.latest_snapshot_id IS NULL
                    OR ms.id != mm.latest_snapshot_id
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM anomalies a
                    WHERE a.latest_snapshot_id = ms.id
                  )
                  AND (
                    NOT :require_chart_history
                    OR EXISTS (
                      SELECT 1
                      FROM market_price_history h
                      WHERE h.market_pk = ms.market_pk
                        AND h.interval_sec = :live_chart_interval_sec
                        AND h.bucket_start = to_timestamp(
                          floor(extract(epoch FROM ms.ts) / :live_chart_interval_sec)
                          * :live_chart_interval_sec
                        )
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM market_price_history h
                      WHERE h.market_pk = ms.market_pk
                        AND h.interval_sec = :compact_chart_interval_sec
                        AND h.bucket_start = to_timestamp(
                          floor(extract(epoch FROM ms.ts) / :compact_chart_interval_sec)
                          * :compact_chart_interval_sec
                        )
                    )
                  )
                """
            ),
            {
                "tier": tier,
                "cutoff": cutoff,
                "require_chart_history": require_chart_history,
                "live_chart_interval_sec": live_chart_interval_sec,
                "compact_chart_interval_sec": compact_chart_interval_sec,
            },
        ).scalar_one()
        or 0
    )


def _delete_snapshots_batch(
    db: Session,
    tier: str,
    cutoff: datetime,
    *,
    batch_size: int,
    require_chart_history: bool,
    live_chart_interval_sec: int,
    compact_chart_interval_sec: int,
) -> int:
    result = db.execute(
        text(
            """
            WITH tier_markets AS MATERIALIZED (
                SELECT market_pk, latest_snapshot_id
                FROM market_metrics
                WHERE storage_tier = :tier
            ),
            candidate_ids AS MATERIALIZED (
                SELECT s.id
                FROM tier_markets mm
                JOIN LATERAL (
                    SELECT ms.id
                    FROM market_snapshots ms
                    WHERE ms.market_pk = mm.market_pk
                      AND ms.ts < :cutoff
                      AND (
                        mm.latest_snapshot_id IS NULL
                        OR ms.id != mm.latest_snapshot_id
                      )
                      AND NOT EXISTS (
                        SELECT 1
                        FROM anomalies a
                        WHERE a.latest_snapshot_id = ms.id
                      )
                      AND (
                        NOT :require_chart_history
                        OR EXISTS (
                          SELECT 1
                          FROM market_price_history h
                          WHERE h.market_pk = ms.market_pk
                            AND h.interval_sec = :live_chart_interval_sec
                            AND h.bucket_start = to_timestamp(
                              floor(extract(epoch FROM ms.ts) / :live_chart_interval_sec)
                              * :live_chart_interval_sec
                            )
                        )
                        OR EXISTS (
                          SELECT 1
                          FROM market_price_history h
                          WHERE h.market_pk = ms.market_pk
                            AND h.interval_sec = :compact_chart_interval_sec
                            AND h.bucket_start = to_timestamp(
                              floor(extract(epoch FROM ms.ts) / :compact_chart_interval_sec)
                              * :compact_chart_interval_sec
                            )
                        )
                      )
                    ORDER BY ms.ts ASC, ms.id ASC
                    LIMIT 8
                ) s ON TRUE
                LIMIT :batch_size
            )
            DELETE FROM market_snapshots ms
            USING candidate_ids c
            WHERE ms.id = c.id
            """
        ),
        {
            "tier": tier,
            "cutoff": cutoff,
            "batch_size": batch_size,
            "require_chart_history": require_chart_history,
            "live_chart_interval_sec": live_chart_interval_sec,
            "compact_chart_interval_sec": compact_chart_interval_sec,
        },
    )
    return int(result.rowcount or 0)


def _count_trades(
    db: Session,
    tier: str,
    close_cutoff: datetime,
    *,
    require_chart_history: bool,
    live_chart_interval_sec: int,
    compact_chart_interval_sec: int,
    delete_flagged_with_evidence: bool,
) -> int:
    return int(
        db.execute(
            text(
                f"""
                SELECT count(*)::bigint
                FROM trades t
                JOIN markets m ON m.id = t.market_pk
                JOIN market_metrics mm ON mm.market_pk = t.market_pk
                WHERE mm.storage_tier = :tier
                  AND {_CLOSED_MARKET_STATUS_SQL}
                  AND (
                    NOT EXISTS (
                      SELECT 1 FROM trade_flags tf WHERE tf.trade_pk = t.id
                    )
                    OR (
                      :delete_flagged_with_evidence
                      AND NOT EXISTS (
                        SELECT 1
                        FROM trade_flags tf
                        WHERE tf.trade_pk = t.id
                          AND NOT EXISTS (
                            SELECT 1
                            FROM trade_evidence te
                            WHERE te.trade_id = tf.trade_id
                              AND te.scorer_version = tf.scorer_version
                          )
                      )
                    )
                  )
                  AND (
                    NOT :require_chart_history
                    OR EXISTS (
                      SELECT 1
                      FROM market_price_history h
                      WHERE h.market_pk = t.market_pk
                        AND h.interval_sec = :live_chart_interval_sec
                        AND h.bucket_start = to_timestamp(
                          floor(extract(epoch FROM t.ts) / :live_chart_interval_sec)
                          * :live_chart_interval_sec
                        )
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM market_price_history h
                      WHERE h.market_pk = t.market_pk
                        AND h.interval_sec = :compact_chart_interval_sec
                        AND h.bucket_start = to_timestamp(
                          floor(extract(epoch FROM t.ts) / :compact_chart_interval_sec)
                          * :compact_chart_interval_sec
                        )
                    )
                  )
                """
            ),
            {
                "tier": tier,
                "close_cutoff": close_cutoff,
                "require_chart_history": require_chart_history,
                "live_chart_interval_sec": live_chart_interval_sec,
                "compact_chart_interval_sec": compact_chart_interval_sec,
                "delete_flagged_with_evidence": delete_flagged_with_evidence,
            },
        ).scalar_one()
        or 0
    )


def _materialize_trade_evidence_batch(
    db: Session,
    tier: str,
    close_cutoff: datetime,
    *,
    batch_size: int,
) -> int:
    row = (
        db.execute(
            text(
                f"""
                WITH candidates AS MATERIALIZED (
                    SELECT
                        tf.id AS source_trade_flag_pk,
                        t.id AS source_trade_pk,
                        t.trade_id,
                        t.market_pk,
                        t.ts,
                        t.yes_price_dollars,
                        t.no_price_dollars,
                        t.count_fp,
                        t.taker_side,
                        tf.score,
                        tf.local_score,
                        tf.context_score,
                        tf.severity,
                        tf.reasons,
                        tf.components,
                        tf.features,
                        tf.scorer_version,
                        mm.storage_tier
                    FROM trade_flags tf
                    JOIN trades t ON t.id = tf.trade_pk
                    JOIN markets m ON m.id = t.market_pk
                    JOIN market_metrics mm ON mm.market_pk = t.market_pk
                    WHERE mm.storage_tier = :tier
                      AND {_CLOSED_MARKET_STATUS_SQL}
                      AND NOT EXISTS (
                        SELECT 1
                        FROM trade_evidence te
                        WHERE te.trade_id = tf.trade_id
                          AND te.scorer_version = tf.scorer_version
                      )
                    ORDER BY tf.score DESC, tf.ts DESC, tf.id DESC
                    LIMIT :batch_size
                ),
                inserted AS (
                    INSERT INTO trade_evidence (
                        market_pk,
                        source_trade_pk,
                        source_trade_flag_pk,
                        trade_id,
                        ts,
                        yes_price_dollars,
                        no_price_dollars,
                        count_fp,
                        taker_side,
                        score,
                        local_score,
                        context_score,
                        severity,
                        reasons,
                        components,
                        features,
                        scorer_version,
                        storage_tier,
                        retention_reason,
                        created_at,
                        updated_at
                    )
                    SELECT
                        market_pk,
                        source_trade_pk,
                        source_trade_flag_pk,
                        trade_id,
                        ts,
                        yes_price_dollars,
                        no_price_dollars,
                        count_fp,
                        taker_side,
                        score,
                        local_score,
                        context_score,
                        severity,
                        coalesce(reasons, '[]'::json),
                        coalesce(components, '{{}}'::json),
                        coalesce(features, '{{}}'::json),
                        scorer_version,
                        storage_tier,
                        'raw_trade_retention',
                        now(),
                        now()
                    FROM candidates
                    ON CONFLICT (trade_id, scorer_version) DO UPDATE SET
                        source_trade_pk = excluded.source_trade_pk,
                        source_trade_flag_pk = excluded.source_trade_flag_pk,
                        score = greatest(trade_evidence.score, excluded.score),
                        local_score = excluded.local_score,
                        context_score = excluded.context_score,
                        severity = excluded.severity,
                        reasons = excluded.reasons,
                        components = excluded.components,
                        features = excluded.features,
                        storage_tier = excluded.storage_tier,
                        updated_at = now()
                    RETURNING id
                )
                SELECT count(*)::bigint AS upserted FROM inserted
                """
            ),
            {
                "tier": tier,
                "close_cutoff": close_cutoff,
                "batch_size": batch_size,
            },
        )
        .mappings()
        .one()
    )
    return int(row["upserted"] or 0)


def _delete_trades_batch(
    db: Session,
    tier: str,
    close_cutoff: datetime,
    *,
    batch_size: int,
    require_chart_history: bool,
    live_chart_interval_sec: int,
    compact_chart_interval_sec: int,
    delete_flagged_with_evidence: bool,
) -> int:
    result = db.execute(
        text(
            f"""
            WITH candidate_ids AS MATERIALIZED (
                SELECT t.id
                FROM trades t
                JOIN markets m ON m.id = t.market_pk
                JOIN market_metrics mm ON mm.market_pk = t.market_pk
                WHERE mm.storage_tier = :tier
                  AND {_CLOSED_MARKET_STATUS_SQL}
                  AND (
                    NOT EXISTS (
                      SELECT 1 FROM trade_flags tf WHERE tf.trade_pk = t.id
                    )
                    OR (
                      :delete_flagged_with_evidence
                      AND NOT EXISTS (
                        SELECT 1
                        FROM trade_flags tf
                        WHERE tf.trade_pk = t.id
                          AND NOT EXISTS (
                            SELECT 1
                            FROM trade_evidence te
                            WHERE te.trade_id = tf.trade_id
                              AND te.scorer_version = tf.scorer_version
                          )
                      )
                    )
                  )
                  AND (
                    NOT :require_chart_history
                    OR EXISTS (
                      SELECT 1
                      FROM market_price_history h
                      WHERE h.market_pk = t.market_pk
                        AND h.interval_sec = :live_chart_interval_sec
                        AND h.bucket_start = to_timestamp(
                          floor(extract(epoch FROM t.ts) / :live_chart_interval_sec)
                          * :live_chart_interval_sec
                        )
                    )
                    OR EXISTS (
                      SELECT 1
                      FROM market_price_history h
                      WHERE h.market_pk = t.market_pk
                        AND h.interval_sec = :compact_chart_interval_sec
                        AND h.bucket_start = to_timestamp(
                          floor(extract(epoch FROM t.ts) / :compact_chart_interval_sec)
                          * :compact_chart_interval_sec
                        )
                    )
                  )
                ORDER BY t.ts ASC, t.id ASC
                LIMIT :batch_size
            )
            DELETE FROM trades t
            USING candidate_ids c
            WHERE t.id = c.id
            """
        ),
        {
            "tier": tier,
            "close_cutoff": close_cutoff,
            "batch_size": batch_size,
            "require_chart_history": require_chart_history,
            "live_chart_interval_sec": live_chart_interval_sec,
            "compact_chart_interval_sec": compact_chart_interval_sec,
            "delete_flagged_with_evidence": delete_flagged_with_evidence,
        },
    )
    return int(result.rowcount or 0)


def _run_batched_delete(
    db: Session,
    *,
    count_before: int | None = None,
    execute: bool,
    batch_size: int,
    delete_batch,
    sleep_seconds: float = 0.25,
    max_batches: int = 20,
    stop_on_partial_batch: bool = True,
) -> dict[str, int]:
    # In dry-run mode, we can report the pre-count if the caller chose to compute it.
    # In execute mode, avoid expensive exact counts and just run bounded delete batches.
    if not execute:
        return {
            "matched": int(count_before or 0),
            "deleted": 0,
            "batches": 0,
        }

    deleted = 0
    batches = 0

    while batches < max_batches:
        n = delete_batch()
        if n <= 0:
            break

        deleted += n
        batches += 1
        db.commit()

        if stop_on_partial_batch and n < batch_size:
            break

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return {
        # Without an exact pre-count, "matched" should not pretend to be total eligible.
        # Use deleted as the observed matched/deleted amount for this bounded sweep.
        "matched": int(count_before) if count_before is not None else deleted,
        "deleted": deleted,
        "batches": batches,
    }


def run_retention_maintenance(
    db: Session,
    *,
    execute: bool = False,
    book_event_days: int = settings.retention_book_events_max_age_days,
    observe_snapshot_days: int = settings.retention_snapshot_observe_max_age_days,
    sampled_snapshot_days: int = settings.retention_snapshot_sampled_max_age_days,
    hot_snapshot_days: int = settings.retention_snapshot_hot_max_age_days,
    batch_size: int = settings.retention_batch_size,
    max_batches: int = DEFAULT_RETENTION_MAX_BATCHES,
    analyze: bool = False,
    require_chart_history_for_snapshots: bool = True,
    trade_retention_enabled: bool = False,
    trade_retention_days_by_tier: dict[str, int] | None = None,
    require_chart_history_for_trades: bool = True,
    delete_flagged_trades_with_evidence: bool = False,
    exact_trade_dry_run_counts: bool = False,
    live_chart_interval_sec: int = DEFAULT_CHART_HISTORY_INTERVAL_SEC,
    compact_chart_interval_sec: int = DEFAULT_COMPACT_CHART_HISTORY_INTERVAL_SEC,
) -> dict[str, Any]:
    started = time.monotonic()
    batch_size = max(1, batch_size)
    max_batches = max(1, max_batches)
    book_cutoff = _cutoff(book_event_days)
    logger.info(
        "retention maintenance started execute=%s batch_size=%s max_batches=%s",
        execute,
        batch_size,
        max_batches,
    )
    book_started = time.monotonic()
    book_count = _count_book_events(db, book_cutoff)
    book_result = _run_batched_delete(
        db,
        count_before=book_count,
        execute=execute,
        batch_size=batch_size,
        delete_batch=lambda: _delete_book_events_batch(
            db, book_cutoff, batch_size=batch_size
        ),
        max_batches=max_batches,
        stop_on_partial_batch=True,
    )
    book_result["duration_seconds"] = round(time.monotonic() - book_started, 3)
    logger.info(
        "retention table=book_events matched=%s deleted=%s batches=%s duration_seconds=%.3f",
        book_result["matched"],
        book_result["deleted"],
        book_result["batches"],
        book_result["duration_seconds"],
    )

    snapshot_specs = {
        "observe_only": observe_snapshot_days,
        "sampled": sampled_snapshot_days,
        "hot": hot_snapshot_days,
    }
    snapshot_results: dict[str, dict[str, Any]] = {}
    for tier, days in snapshot_specs.items():
        cutoff = _cutoff(days)
        tier_started = time.monotonic()

        # Exact snapshot counts are very expensive on the large market_snapshots table.
        # Only compute them for dry-run reporting. In execute mode, just run bounded
        # delete batches and report the actual deleted rows.
        count = None if execute else _count_snapshots(
            db,
            tier,
            cutoff,
            require_chart_history=require_chart_history_for_snapshots,
            live_chart_interval_sec=live_chart_interval_sec,
            compact_chart_interval_sec=compact_chart_interval_sec,
        )

        result = _run_batched_delete(
            db,
            count_before=count,
            execute=execute,
            batch_size=batch_size,
            delete_batch=lambda tier=tier, cutoff=cutoff: _delete_snapshots_batch(
                db,
                tier,
                cutoff,
                batch_size=batch_size,
                require_chart_history=require_chart_history_for_snapshots,
                live_chart_interval_sec=live_chart_interval_sec,
                compact_chart_interval_sec=compact_chart_interval_sec,
            ),
            max_batches=max_batches,
            stop_on_partial_batch=False,
        )
        snapshot_results[tier] = {
            **result,
            "cutoff": cutoff.isoformat(),
            "max_age_days": days,
            "duration_seconds": round(time.monotonic() - tier_started, 3),
            "remaining_eligible_estimate": None if execute else int(count or 0),
            "remaining_estimate_reason": (
                "not_computed_in_execute_mode"
                if execute
                else "exact_dry_run_count_before_delete"
            ),
        }
        logger.info(
            "retention tier=%s matched=%s deleted=%s batches=%s duration_seconds=%.3f",
            tier,
            snapshot_results[tier]["matched"],
            snapshot_results[tier]["deleted"],
            snapshot_results[tier]["batches"],
            snapshot_results[tier]["duration_seconds"],
        )

    trade_specs = trade_retention_days_by_tier or TRADE_RETENTION_DAYS_BY_TIER
    trade_results: dict[str, dict[str, Any]] = {}
    if trade_retention_enabled:
        for tier, days in trade_specs.items():
            cutoff = _cutoff(days)
            tier_started = time.monotonic()
            evidence_total = 0
            if execute:
                for _ in range(max_batches):
                    evidence_count = _materialize_trade_evidence_batch(
                        db,
                        tier,
                        cutoff,
                        batch_size=batch_size,
                    )
                    if evidence_count <= 0:
                        break
                    evidence_total += evidence_count
                    db.commit()
                    time.sleep(0.05)

            count = (
                None
                if execute or not exact_trade_dry_run_counts
                else _count_trades(
                    db,
                    tier,
                    cutoff,
                    require_chart_history=require_chart_history_for_trades,
                    live_chart_interval_sec=live_chart_interval_sec,
                    compact_chart_interval_sec=compact_chart_interval_sec,
                    delete_flagged_with_evidence=delete_flagged_trades_with_evidence,
                )
            )
            result = _run_batched_delete(
                db,
                count_before=count,
                execute=execute,
                batch_size=batch_size,
                delete_batch=lambda tier=tier, cutoff=cutoff: _delete_trades_batch(
                    db,
                    tier,
                    cutoff,
                    batch_size=batch_size,
                    require_chart_history=require_chart_history_for_trades,
                    live_chart_interval_sec=live_chart_interval_sec,
                    compact_chart_interval_sec=compact_chart_interval_sec,
                    delete_flagged_with_evidence=delete_flagged_trades_with_evidence,
                ),
                max_batches=max_batches,
                stop_on_partial_batch=False,
            )
            trade_results[tier] = {
                **result,
                "evidence_upserted": evidence_total,
                "cutoff": cutoff.isoformat(),
                "days_after_close": days,
                "duration_seconds": round(time.monotonic() - tier_started, 3),
                "delete_flagged_with_evidence": delete_flagged_trades_with_evidence,
                "remaining_eligible_estimate": None if execute else int(count or 0),
                "remaining_estimate_reason": (
                    "not_computed_in_execute_mode"
                    if execute
                    else (
                        "exact_dry_run_count_before_delete"
                        if exact_trade_dry_run_counts
                        else "not_computed_without_exact_trade_dry_run_counts"
                    )
                ),
            }
            logger.info(
                "retention trades tier=%s evidence=%s matched=%s deleted=%s "
                "batches=%s duration_seconds=%.3f",
                tier,
                evidence_total,
                trade_results[tier]["matched"],
                trade_results[tier]["deleted"],
                trade_results[tier]["batches"],
                trade_results[tier]["duration_seconds"],
            )

    if execute:
        db.commit()
        if analyze:
            db.execute(text("analyze book_events"))
            db.execute(text("analyze market_snapshots"))
            if trade_retention_enabled:
                db.execute(text("analyze trades"))
                db.execute(text("analyze trade_evidence"))
            db.commit()
    else:
        db.rollback()

    deleted_total = int(book_result["deleted"]) + sum(
        int(result["deleted"]) for result in snapshot_results.values()
    ) + sum(
        int(result["deleted"]) for result in trade_results.values()
    )
    matched_total = int(book_result["matched"]) + sum(
        int(result["matched"]) for result in snapshot_results.values()
    ) + sum(
        int(result["matched"]) for result in trade_results.values()
    )
    duration_seconds = round(time.monotonic() - started, 3)
    logger.info(
        "retention maintenance finished execute=%s deleted_total=%s matched_total=%s duration_seconds=%.3f",
        execute,
        deleted_total,
        matched_total,
        duration_seconds,
    )
    return {
        "execute": execute,
        "batch_size": batch_size,
        "max_batches": max_batches,
        "duration_seconds": duration_seconds,
        "matched_total": matched_total,
        "deleted_total": deleted_total,
        "require_chart_history_for_snapshots": require_chart_history_for_snapshots,
        "trade_retention_enabled": trade_retention_enabled,
        "require_chart_history_for_trades": require_chart_history_for_trades,
        "delete_flagged_trades_with_evidence": delete_flagged_trades_with_evidence,
        "exact_trade_dry_run_counts": exact_trade_dry_run_counts,
        "live_chart_interval_sec": live_chart_interval_sec,
        "compact_chart_interval_sec": compact_chart_interval_sec,
        "book_events": {
            **book_result,
            "cutoff": book_cutoff.isoformat(),
            "max_age_days": book_event_days,
        },
        "market_snapshots": snapshot_results,
        "trades": trade_results,
        "analyze": bool(analyze and execute),
    }



RETENTION_MAINTENANCE_LOCK_KEY = "predictionmarkets:retention_maintenance"


def _try_retention_maintenance_lock(db: Session) -> bool:
    return bool(
        db.execute(
            text("select pg_try_advisory_lock(hashtext(:key))"),
            {"key": RETENTION_MAINTENANCE_LOCK_KEY},
        ).scalar()
    )


def _release_retention_maintenance_lock(db: Session) -> None:
    db.execute(
        text("select pg_advisory_unlock(hashtext(:key))"),
        {"key": RETENTION_MAINTENANCE_LOCK_KEY},
    )


def _run_once(args: argparse.Namespace) -> dict[str, Any]:
    db = SessionLocal()
    locked = False
    try:
        locked = _try_retention_maintenance_lock(db)
        if not locked:
            logger.info("retention maintenance lock skipped")
            return {
                "execute": args.execute,
                "skipped": True,
                "reason": "another_retention_sweep_running",
                "lock_acquired": False,
                "matched_total": 0,
                "deleted_total": 0,
            }

        logger.info("retention maintenance lock acquired")
        result = run_retention_maintenance(
            db,
            execute=args.execute,
            book_event_days=args.book_event_days,
            observe_snapshot_days=args.observe_snapshot_days,
            sampled_snapshot_days=args.sampled_snapshot_days,
            hot_snapshot_days=args.hot_snapshot_days,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            analyze=args.analyze,
            require_chart_history_for_snapshots=not args.allow_uncovered_snapshot_delete,
            trade_retention_enabled=args.with_trade_retention,
            trade_retention_days_by_tier={
                "observe_only": args.trade_observe_days_after_close,
                "sampled": args.trade_sampled_days_after_close,
                "hot": args.trade_hot_days_after_close,
                "triggered": args.trade_triggered_days_after_close,
                "case": args.trade_case_days_after_close,
            },
            require_chart_history_for_trades=not args.allow_uncovered_trade_delete,
            delete_flagged_trades_with_evidence=args.delete_flagged_trades_with_evidence,
            exact_trade_dry_run_counts=args.exact_trade_dry_run_counts,
            live_chart_interval_sec=args.snapshot_chart_interval_sec,
            compact_chart_interval_sec=args.snapshot_compact_chart_interval_sec,
        )
        result["lock_acquired"] = True
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        if locked:
            _release_retention_maintenance_lock(db)
            logger.info("retention maintenance lock released")
        db.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--book-event-days", type=int, default=settings.retention_book_events_max_age_days)
    parser.add_argument("--observe-snapshot-days", type=int, default=settings.retention_snapshot_observe_max_age_days)
    parser.add_argument("--sampled-snapshot-days", type=int, default=settings.retention_snapshot_sampled_max_age_days)
    parser.add_argument("--hot-snapshot-days", type=int, default=settings.retention_snapshot_hot_max_age_days)
    parser.add_argument("--batch-size", type=int, default=settings.retention_batch_size)
    parser.add_argument("--max-batches", type=int, default=DEFAULT_RETENTION_MAX_BATCHES)
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--with-trade-retention", action="store_true")
    parser.add_argument("--trade-observe-days-after-close", type=int, default=1)
    parser.add_argument("--trade-sampled-days-after-close", type=int, default=14)
    parser.add_argument("--trade-hot-days-after-close", type=int, default=30)
    parser.add_argument("--trade-triggered-days-after-close", type=int, default=90)
    parser.add_argument("--trade-case-days-after-close", type=int, default=365)
    parser.add_argument(
        "--delete-flagged-trades-with-evidence",
        action="store_true",
        help=(
            "Allow deleting flagged raw trades once matching trade_evidence rows "
            "exist. Leave unset to keep flagged raw trades."
        ),
    )
    parser.add_argument(
        "--allow-uncovered-trade-delete",
        action="store_true",
        help=(
            "Allow deleting old trades even when matching chart-history coverage "
            "is missing. Keep unset for conservative production runs."
        ),
    )
    parser.add_argument(
        "--exact-trade-dry-run-counts",
        action="store_true",
        help=(
            "Compute exact trade-retention dry-run counts. This can be expensive "
            "on large trade tables, so it is opt-in."
        ),
    )
    parser.add_argument(
        "--allow-uncovered-snapshot-delete",
        action="store_true",
        help=(
            "Allow deleting old snapshots even when matching chart-history coverage "
            "is missing. Keep unset for conservative production runs."
        ),
    )
    parser.add_argument(
        "--snapshot-chart-interval-sec",
        type=int,
        default=DEFAULT_CHART_HISTORY_INTERVAL_SEC,
    )
    parser.add_argument(
        "--snapshot-compact-chart-interval-sec",
        type=int,
        default=DEFAULT_COMPACT_CHART_HISTORY_INTERVAL_SEC,
    )
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=3600.0)
    args = parser.parse_args()

    while True:
        run_id = new_run_id("retention")
        mark_pipeline_start(
            "retention_maintenance",
            detail="Starting retention maintenance sweep.",
            run_id=run_id,
            metadata={"execute": args.execute},
        )
        try:
            result = _run_once(args)
            if result.get("skipped"):
                detail = "Skipped retention maintenance; another sweep is already running."
                count = 0
            else:
                count = int(result["deleted_total"] if args.execute else result["matched_total"])
                detail = (
                    f"{'Deleted' if args.execute else 'Matched'} "
                    f"{result['deleted_total'] if args.execute else result['matched_total']} "
                    "old hot-table rows."
                )

            mark_pipeline_success(
                "retention_maintenance",
                detail=detail,
                run_id=run_id,
                count=count,
                metadata=result,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
        except Exception as exc:
            logger.exception("retention maintenance sweep failed")
            mark_pipeline_error(
                "retention_maintenance",
                exc,
                detail="Retention maintenance sweep failed.",
                run_id=run_id,
            )
            raise
        if not args.watch:
            return
        time.sleep(max(1.0, args.interval_seconds))


if __name__ == "__main__":
    main()
