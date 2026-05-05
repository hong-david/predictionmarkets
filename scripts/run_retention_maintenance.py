"""Run bounded retention cleanup for bulky Postgres hot tables.

Dry-run is the default. Pass ``--execute`` to delete rows. The script works in
small batches so it can be scheduled frequently without holding long table
locks. It preserves the latest snapshot per market and snapshots referenced by
stored anomalies.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Anomaly, BookEvent, MarketMetric, MarketSnapshot
from app.db.session import SessionLocal
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
)


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


def _snapshot_scope(tier: str, cutoff: datetime):
    referenced_by_anomaly = (
        select(Anomaly.id)
        .where(Anomaly.latest_snapshot_id == MarketSnapshot.id)
        .exists()
    )
    return (
        select(MarketSnapshot.id)
        .join(MarketMetric, MarketMetric.market_pk == MarketSnapshot.market_pk)
        .where(MarketMetric.storage_tier == tier)
        .where(MarketSnapshot.ts < cutoff)
        .where(
            (MarketMetric.latest_snapshot_id.is_(None))
            | (MarketSnapshot.id != MarketMetric.latest_snapshot_id)
        )
        .where(~referenced_by_anomaly)
    )


def _count_snapshots(db: Session, tier: str, cutoff: datetime) -> int:
    scope = _snapshot_scope(tier, cutoff).subquery()
    return int(db.execute(select(func.count()).select_from(scope)).scalar_one() or 0)


def _delete_snapshots_batch(
    db: Session,
    tier: str,
    cutoff: datetime,
    *,
    batch_size: int,
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

        if n < batch_size:
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
    analyze: bool = False,
) -> dict[str, Any]:
    batch_size = max(1, batch_size)
    book_cutoff = _cutoff(book_event_days)
    book_count = _count_book_events(db, book_cutoff)
    book_result = _run_batched_delete(
        db,
        count_before=book_count,
        execute=execute,
        batch_size=batch_size,
        delete_batch=lambda: _delete_book_events_batch(
            db, book_cutoff, batch_size=batch_size
        ),
    )

    snapshot_specs = {
        "observe_only": observe_snapshot_days,
        "sampled": sampled_snapshot_days,
        "hot": hot_snapshot_days,
    }
    snapshot_results: dict[str, dict[str, Any]] = {}
    for tier, days in snapshot_specs.items():
        cutoff = _cutoff(days)

        # Exact snapshot counts are very expensive on the large market_snapshots table.
        # Only compute them for dry-run reporting. In execute mode, just run bounded
        # delete batches and report the actual deleted rows.
        count = None if execute else _count_snapshots(db, tier, cutoff)

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
            ),
        )
        snapshot_results[tier] = {
            **result,
            "cutoff": cutoff.isoformat(),
            "max_age_days": days,
        }

    if execute:
        db.commit()
        if analyze:
            db.execute(text("analyze book_events"))
            db.execute(text("analyze market_snapshots"))
            db.commit()
    else:
        db.rollback()

    deleted_total = int(book_result["deleted"]) + sum(
        int(result["deleted"]) for result in snapshot_results.values()
    )
    matched_total = int(book_result["matched"]) + sum(
        int(result["matched"]) for result in snapshot_results.values()
    )
    return {
        "execute": execute,
        "batch_size": batch_size,
        "matched_total": matched_total,
        "deleted_total": deleted_total,
        "book_events": {
            **book_result,
            "cutoff": book_cutoff.isoformat(),
            "max_age_days": book_event_days,
        },
        "market_snapshots": snapshot_results,
        "analyze": bool(analyze and execute),
    }


def _run_once(args: argparse.Namespace) -> dict[str, Any]:
    db = SessionLocal()
    try:
        return run_retention_maintenance(
            db,
            execute=args.execute,
            book_event_days=args.book_event_days,
            observe_snapshot_days=args.observe_snapshot_days,
            sampled_snapshot_days=args.sampled_snapshot_days,
            hot_snapshot_days=args.hot_snapshot_days,
            batch_size=args.batch_size,
            analyze=args.analyze,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--book-event-days", type=int, default=settings.retention_book_events_max_age_days)
    parser.add_argument("--observe-snapshot-days", type=int, default=settings.retention_snapshot_observe_max_age_days)
    parser.add_argument("--sampled-snapshot-days", type=int, default=settings.retention_snapshot_sampled_max_age_days)
    parser.add_argument("--hot-snapshot-days", type=int, default=settings.retention_snapshot_hot_max_age_days)
    parser.add_argument("--batch-size", type=int, default=settings.retention_batch_size)
    parser.add_argument("--analyze", action="store_true")
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
            mark_pipeline_success(
                "retention_maintenance",
                detail=(
                    f"{'Deleted' if args.execute else 'Matched'} "
                    f"{result['deleted_total'] if args.execute else result['matched_total']} "
                    "old hot-table rows."
                ),
                run_id=run_id,
                count=int(result["deleted_total"] if args.execute else result["matched_total"]),
                metadata=result,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
        except Exception as exc:
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
