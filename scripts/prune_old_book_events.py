"""Delete old `book_events` rows to reclaim space (L2 history is the largest
hot path in many deployments).

**This is destructive** — you lose the ability to re-run 3m book-activity
statistics on the pruned range. The WS consumer already caps which markets
get order-book subscriptions, but the table still grows. Keep snapshots,
trades, and anomalies; only `book_events` is targeted.

Usage::

  # Dry-run: print how many rows would be deleted
  python scripts/prune_old_book_events.py

  # Execute (requires --execute)
  python scripts/prune_old_book_events.py --execute --min-age-days 30

The default is dry-run. Default ``--min-age-days`` is 30.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from app.db.models import BookEvent
from app.db.session import SessionLocal


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--min-age-days",
        type=int,
        default=30,
        help="Delete rows with received_at older than this many days (default 30).",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Actually run DELETE. Without this flag, only a count is printed.",
    )
    args = p.parse_args()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.min_age_days)
    db = SessionLocal()
    try:
        n = (
            db.execute(
                select(func.count())
                .select_from(BookEvent)
                .where(BookEvent.received_at < cutoff)
            )
            .scalar_one()
        ) or 0
        n = int(n)
        print(
            f"book_events with received_at < {cutoff.isoformat()}: {n} row(s) "
            f"(--min-age-days {args.min_age_days})"
        )
        if n == 0:
            return 0
        if not args.execute:
            print("Dry-run. Pass --execute to delete these rows.")
            return 0
        db.execute(delete(BookEvent).where(BookEvent.received_at < cutoff))
        db.commit()
        print("Deleted.")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
