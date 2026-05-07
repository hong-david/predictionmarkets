"""Roll up and prune low-signal anomaly detail rows.

Dry-run is the default. Pass ``--execute`` to summarize and delete rows. The
initial policy is intentionally conservative: old ``none``/``low`` rows are
summarized into daily buckets, while ``medium``/``high``/``critical`` detail
rows remain intact unless an operator explicitly changes ``--severities``.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import bindparam, text

from app.db.session import SessionLocal
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
)

logger = logging.getLogger(__name__)

DEFAULT_SEVERITIES = ("none", "low")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cutoff(days: int) -> datetime:
    return _utc_now() - timedelta(days=min(max(0, days), 100_000))


def _parse_severities(value: str) -> tuple[str, ...]:
    severities = tuple(
        sorted({part.strip().lower() for part in value.split(",") if part.strip()})
    )
    if not severities:
        raise ValueError("at least one severity is required")
    return severities


def _candidate_stmt():
    return text(
        """
        SELECT
            count(*)::bigint AS candidate_count,
            min(created_at) AS first_created_at,
            max(created_at) AS last_created_at,
            NULL::bigint AS affected_markets
        FROM anomalies
        WHERE created_at < :cutoff
          AND severity IN :severities
        """
    ).bindparams(bindparam("severities", expanding=True))


def _execute_batch_stmt():
    return text(
        """
        WITH victims AS MATERIALIZED (
            SELECT
                id,
                market_pk,
                date_trunc('day', created_at)::date AS summary_date,
                severity,
                score::numeric AS score,
                created_at
            FROM anomalies
            WHERE created_at < :cutoff
              AND severity IN :severities
            ORDER BY severity ASC, created_at ASC, id ASC
            LIMIT :batch_size
        ),
        rolled AS (
            INSERT INTO anomaly_daily_summaries (
                market_pk,
                summary_date,
                severity,
                anomaly_count,
                max_score,
                avg_score,
                first_created_at,
                last_created_at,
                latest_anomaly_id,
                created_at,
                updated_at
            )
            SELECT
                market_pk,
                summary_date,
                severity,
                count(*)::bigint,
                max(score),
                avg(score),
                min(created_at),
                max(created_at),
                (array_agg(id ORDER BY created_at DESC, id DESC))[1],
                now(),
                now()
            FROM victims
            GROUP BY market_pk, summary_date, severity
            ON CONFLICT (market_pk, summary_date, severity) DO UPDATE SET
                avg_score = (
                    (
                        anomaly_daily_summaries.avg_score
                        * anomaly_daily_summaries.anomaly_count
                    )
                    + (excluded.avg_score * excluded.anomaly_count)
                ) / nullif(
                    anomaly_daily_summaries.anomaly_count + excluded.anomaly_count,
                    0
                ),
                anomaly_count = anomaly_daily_summaries.anomaly_count
                    + excluded.anomaly_count,
                max_score = greatest(
                    anomaly_daily_summaries.max_score,
                    excluded.max_score
                ),
                first_created_at = least(
                    anomaly_daily_summaries.first_created_at,
                    excluded.first_created_at
                ),
                last_created_at = greatest(
                    anomaly_daily_summaries.last_created_at,
                    excluded.last_created_at
                ),
                latest_anomaly_id = CASE
                    WHEN excluded.last_created_at
                        >= anomaly_daily_summaries.last_created_at
                    THEN excluded.latest_anomaly_id
                    ELSE anomaly_daily_summaries.latest_anomaly_id
                END,
                updated_at = now()
            RETURNING market_pk
        ),
        deleted AS (
            DELETE FROM anomalies a
            USING victims v
            WHERE a.id = v.id
            RETURNING a.market_pk
        ),
        deleted_by_market AS (
            SELECT
                market_pk,
                count(*)::bigint AS deleted_count
            FROM deleted
            GROUP BY market_pk
        ),
        metric_update AS (
            UPDATE market_metrics mm
            SET
                anomaly_count = greatest(
                    coalesce(mm.anomaly_count, 0) - deleted_by_market.deleted_count,
                    coalesce(mm.high_anomaly_count, 0),
                    0
                ),
                updated_at = now()
            FROM deleted_by_market
            WHERE mm.market_pk = deleted_by_market.market_pk
            RETURNING mm.market_pk
        )
        SELECT
            (SELECT count(*) FROM victims)::bigint AS victim_count,
            (SELECT count(*) FROM deleted)::bigint AS deleted_count,
            (SELECT count(DISTINCT market_pk) FROM deleted)::bigint
                AS affected_markets,
            (SELECT count(*) FROM metric_update)::bigint AS metrics_updated
        """
    ).bindparams(bindparam("severities", expanding=True))


def _candidate_snapshot(cutoff: datetime, severities: tuple[str, ...]) -> dict[str, Any]:
    db = SessionLocal()
    try:
        row = (
            db.execute(
                _candidate_stmt(),
                {"cutoff": cutoff, "severities": severities},
            )
            .mappings()
            .one()
        )
        return dict(row)
    finally:
        db.close()


def run_anomaly_retention(
    *,
    execute: bool = False,
    cutoff_days: int = 7,
    severities: tuple[str, ...] = DEFAULT_SEVERITIES,
    batch_size: int = 5_000,
    max_batches: int = 100,
    analyze: bool = False,
    sleep_seconds: float = 0.0,
) -> dict[str, Any]:
    cutoff = _cutoff(cutoff_days)
    started = time.monotonic()
    before = _candidate_snapshot(cutoff, severities)
    logger.info(
        "anomaly retention started execute=%s cutoff_days=%s severities=%s "
        "candidates=%s affected_markets=%s batch_size=%s max_batches=%s",
        execute,
        cutoff_days,
        ",".join(severities),
        before["candidate_count"],
        before["affected_markets"],
        batch_size,
        max_batches,
    )

    deleted_total = 0
    affected_total = 0
    metrics_updated_total = 0
    batches = 0

    if execute:
        for batch_number in range(1, max_batches + 1):
            db = SessionLocal()
            try:
                row = (
                    db.execute(
                        _execute_batch_stmt(),
                        {
                            "cutoff": cutoff,
                            "severities": severities,
                            "batch_size": batch_size,
                        },
                    )
                    .mappings()
                    .one()
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

            deleted = int(row["deleted_count"] or 0)
            if deleted == 0:
                break
            batches = batch_number
            deleted_total += deleted
            affected_total += int(row["affected_markets"] or 0)
            metrics_updated_total += int(row["metrics_updated"] or 0)
            logger.info(
                "anomaly retention batch=%s deleted=%s affected_markets=%s "
                "metrics_updated=%s",
                batch_number,
                deleted,
                row["affected_markets"],
                row["metrics_updated"],
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        if analyze and deleted_total:
            db = SessionLocal()
            try:
                db.execute(text("ANALYZE anomalies"))
                db.execute(text("ANALYZE anomaly_daily_summaries"))
                db.execute(text("ANALYZE market_metrics"))
                db.commit()
            finally:
                db.close()

    after = _candidate_snapshot(cutoff, severities)
    duration = time.monotonic() - started
    result = {
        "execute": execute,
        "cutoff_days": cutoff_days,
        "cutoff": cutoff.isoformat(),
        "severities": list(severities),
        "candidate_count_before": int(before["candidate_count"] or 0),
        "candidate_count_after": int(after["candidate_count"] or 0),
        "deleted_total": deleted_total,
        "affected_market_batches": affected_total,
        "metrics_updated_batches": metrics_updated_total,
        "batches": batches,
        "batch_size": batch_size,
        "max_batches": max_batches,
        "duration_seconds": round(duration, 3),
    }
    logger.info("anomaly retention finished result=%s", result)
    return result


def _run_once(args: argparse.Namespace, *, run_id: str) -> dict[str, Any]:
    severities = _parse_severities(args.severities)
    mark_pipeline_start(
        "anomaly_retention",
        detail="Starting anomaly retention sweep.",
        run_id=run_id,
        metadata={
            "execute": args.execute,
            "cutoff_days": args.cutoff_days,
            "severities": list(severities),
            "batch_size": args.batch_size,
            "max_batches": args.max_batches,
        },
    )
    result = run_anomaly_retention(
        execute=args.execute,
        cutoff_days=args.cutoff_days,
        severities=severities,
        batch_size=args.batch_size,
        max_batches=args.max_batches,
        analyze=args.analyze,
        sleep_seconds=args.sleep_seconds,
    )
    detail = (
        f"Deleted {result['deleted_total']} low-signal anomaly rows."
        if args.execute
        else f"Dry-run found {result['candidate_count_before']} candidate rows."
    )
    mark_pipeline_success(
        "anomaly_retention",
        detail=detail,
        run_id=run_id,
        count=result["deleted_total"] if args.execute else result["candidate_count_before"],
        metadata=result,
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--cutoff-days", type=int, default=7)
    parser.add_argument("--severities", default=",".join(DEFAULT_SEVERITIES))
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument("--max-batches", type=int, default=100)
    parser.add_argument("--sleep-seconds", type=float, default=0.0)
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=21_600)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    while True:
        run_id = new_run_id("anomaly-retention")
        try:
            _run_once(args, run_id=run_id)
        except Exception as exc:
            logger.exception("anomaly retention sweep failed")
            mark_pipeline_error(
                "anomaly_retention",
                exc,
                detail="Anomaly retention sweep failed.",
                run_id=run_id,
            )
            if not args.watch:
                raise

        if not args.watch:
            return
        time.sleep(max(60, args.interval_seconds))


if __name__ == "__main__":
    main()
