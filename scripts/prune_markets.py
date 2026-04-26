"""Delete out-of-scope markets and their child rows.

Reads the scope policy from `app/services/retention.py` and uses the
DB-level `ON DELETE CASCADE` (added in migration `f1a2c3d4b5e6`) so a
single `DELETE FROM markets WHERE ...` cleans up every dependent row in
`market_snapshots`, `trades`, `book_events`, and `anomalies`.

Why a script and not just a SQL one-liner:

  - Default to `--dry-run`. The first thing this script does on a fresh
    DB is propose deleting ~285k rows. We want operators staring at a
    diff before they run it.
  - Print a categorised breakdown so the operator can sanity-check what
    they're about to lose.
  - Use the same predicates as the ingestor — there's exactly one place
    that decides "is this in scope", and it's `retention.py`.

Usage:

  # See what would be deleted (no writes):
  python scripts/prune_markets.py

  # Actually delete:
  python scripts/prune_markets.py --execute

  # Limit to a single category for a partial cleanup:
  python scripts/prune_markets.py --execute --category exotic_combo

  # Or limit by manipulability prior:
  python scripts/prune_markets.py --execute --prior very_low

  # Drop unresolved WebSocket stub rows that still have no exchange metadata:
  python scripts/prune_markets.py --stale-unknown
  python scripts/prune_markets.py --stale-unknown --execute
"""

from __future__ import annotations

import argparse
import sys
import time

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Anomaly, BookEvent, Market, MarketSnapshot, Trade
from app.db.session import SessionLocal
from app.services.retention import EXCLUDED_CATEGORIES, EXCLUDED_PRIORS


def _scope_filter(category: str | None, prior: str | None):
    """Return a SQLAlchemy WHERE clause matching out-of-scope rows.

    `category` / `prior` arguments narrow the policy to a single bucket
    (used by `--category` / `--prior` flags). When both are None the
    full retention policy applies — this is what the operator sees on a
    bare `python scripts/prune_markets.py` invocation.
    """
    cats = (category,) if category else tuple(EXCLUDED_CATEGORIES)
    pris = (prior,) if prior else tuple(EXCLUDED_PRIORS)

    if category and prior:
        return and_(Market.category == category, Market.manipulability_prior == prior)
    if category:
        return Market.category.in_(cats)
    if prior:
        return Market.manipulability_prior.in_(pris)
    return or_(
        Market.category.in_(cats),
        Market.manipulability_prior.in_(pris),
    )


def _stale_unknown_filter():
    return (Market.status == "unknown") & (Market.title == Market.market_id)


def _delete_stale_unknown_batch(db: Session, batch_size: int) -> int:
    ids = [
        row[0]
        for row in db.execute(
            select(Market.id)
            .where(_stale_unknown_filter())
            .order_by(Market.id)
            .limit(batch_size)
        ).all()
    ]
    if not ids:
        return 0

    for model in (Anomaly, BookEvent, Trade, MarketSnapshot):
        db.execute(delete(model).where(model.market_pk.in_(ids)))
    deleted = (
        db.query(Market)
        .filter(Market.id.in_(ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return int(deleted or 0)


def _summarise(db: Session, where) -> None:
    """Print categorised counts of what's about to be deleted."""
    total = db.execute(
        select(func.count()).select_from(Market).where(where)
    ).scalar_one()

    by_cat = db.execute(
        select(Market.category, func.count())
        .where(where)
        .group_by(Market.category)
        .order_by(func.count().desc())
    ).all()

    by_prior = db.execute(
        select(Market.manipulability_prior, func.count())
        .where(where)
        .group_by(Market.manipulability_prior)
        .order_by(func.count().desc())
    ).all()

    print(f"  total markets matched: {total:>10,}")
    print()
    print("  by category:")
    for cat, n in by_cat:
        print(f"    {(cat or '<null>'):<30s} {n:>10,}")
    print()
    print("  by manipulability_prior:")
    for pri, n in by_prior:
        print(f"    {(pri or '<null>'):<16s} {n:>10,}")

    market_ids = select(Market.id).where(where).subquery()
    child_counts = {
        "market_snapshots": db.execute(
            select(func.count())
            .select_from(MarketSnapshot)
            .join(market_ids, market_ids.c.id == MarketSnapshot.market_pk)
        ).scalar_one(),
        "trades": db.execute(
            select(func.count())
            .select_from(Trade)
            .join(market_ids, market_ids.c.id == Trade.market_pk)
        ).scalar_one(),
        "book_events": db.execute(
            select(func.count())
            .select_from(BookEvent)
            .join(market_ids, market_ids.c.id == BookEvent.market_pk)
        ).scalar_one(),
        "anomalies": db.execute(
            select(func.count())
            .select_from(Anomaly)
            .join(market_ids, market_ids.c.id == Anomaly.market_pk)
        ).scalar_one(),
    }
    print()
    print("  child rows that will cascade:")
    for table, n in child_counts.items():
        print(f"    {table:<30s} {int(n):>10,}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete (default is dry-run).",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="Narrow deletion to a single category (e.g. exotic_combo).",
    )
    parser.add_argument(
        "--prior",
        default=None,
        help="Narrow deletion to a single manipulability prior (e.g. very_low).",
    )
    parser.add_argument(
        "--stale-unknown",
        action="store_true",
        help="Delete unresolved WS stub rows: status='unknown' and title=market_id.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="For --stale-unknown --execute, delete this many markets per transaction.",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        where = (
            _stale_unknown_filter()
            if args.stale_unknown
            else _scope_filter(args.category, args.prior)
        )

        print("=== PRUNE PLAN ===")
        if args.stale_unknown:
            print("  filter: status='unknown' AND title=market_id")
        elif args.category or args.prior:
            print(f"  filter: category={args.category!r} prior={args.prior!r}")
        else:
            print(
                f"  filter: category in {sorted(EXCLUDED_CATEGORIES)} "
                f"OR manipulability_prior in {sorted(EXCLUDED_PRIORS)}"
            )
        print()
        _summarise(db, where)
        print()

        if not args.execute:
            print("DRY RUN — no rows deleted. Re-run with --execute to commit.")
            return 0

        print("=== EXECUTING DELETE (cascade to children) ===")
        t0 = time.time()
        # SQLAlchemy synchronize_session=False is essential here — the
        # ORM otherwise tries to keep its identity map consistent across
        # every cascaded child row, which collapses on a 285k delete.
        if args.stale_unknown:
            total_deleted = 0
            while True:
                deleted = _delete_stale_unknown_batch(db, args.batch_size)
                if deleted == 0:
                    break
                total_deleted += deleted
                print(f"  deleted {total_deleted:,} stale unknown markets...", flush=True)
            elapsed = time.time() - t0
            print(
                f"  deleted {total_deleted:,} markets (+ child rows) in {elapsed:.1f}s"
            )
            return 0

        deleted = (
            db.query(Market)
            .filter(where)
            .delete(synchronize_session=False)
        )
        db.commit()
        elapsed = time.time() - t0
        print(f"  deleted {deleted:,} markets (+ cascaded children) in {elapsed:.1f}s")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
