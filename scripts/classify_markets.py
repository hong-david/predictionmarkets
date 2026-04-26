"""Backfill / reclassify markets via the layered classifier.

Use cases:
  1. After running the migration that adds the classifier columns, every
     existing row has `classifier_version IS NULL`. Run this script with no
     args to populate them all.
  2. After bumping `CLASSIFIER_VERSION` (rule changes, new layer), run
     this script to reclassify rows whose stored verdict is stale.
  3. Re-run on demand after editing seeds / rules during development.

Why a script, not an automatic on-startup migration:
  Reclassification can touch hundreds of thousands of rows, and the LLM
  layer is rate-limited. We want this to be an explicit, observable
  operation — printable progress, ctrl-C friendly, optional `--max` for
  smoke runs — rather than a hidden side effect of `alembic upgrade`.

Operational notes:
  - Layer 1 (Kalshi taxonomy) needs the *raw* Kalshi market dict, which
    we don't persist. So this script's classifications come purely from
    Layers 2-4 (ticker prefix rules, k-NN, optional LLM). The REST poller
    is the path that gets Layer 1 information; on the next poll, those
    rows will reclassify with Layer 1 verdicts and overwrite this script's
    output. That's fine and expected.
  - `--top-by-trades` mirrors the lazy-upsert hydrator: prioritise rows
    the dashboard surfaces.
"""

from __future__ import annotations

import argparse
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Market, Trade
from app.db.session import SessionLocal
from app.services.classifier import CLASSIFIER_VERSION, classify


def select_targets(
    db: Session,
    *,
    top_by_trades: bool = False,
    max_markets: int | None = None,
    only_stale: bool = True,
) -> list[Market]:
    """Pick the rows to reclassify.

    `only_stale=True` (default) limits to rows whose stored
    `classifier_version` is None or older than `CLASSIFIER_VERSION`.
    """
    stmt = select(Market)
    if only_stale:
        stmt = stmt.where(
            (Market.classifier_version.is_(None))
            | (Market.classifier_version < CLASSIFIER_VERSION)
        )

    if top_by_trades:
        # Order by descending trade count so dashboard-visible markets
        # get reclassified first. Same shape as hydrate_unknown_markets.py.
        trade_count = (
            select(Trade.market_pk, func.count(Trade.id).label("trade_count"))
            .group_by(Trade.market_pk)
            .subquery()
        )
        stmt = (
            stmt.outerjoin(trade_count, trade_count.c.market_pk == Market.id)
            .order_by(func.coalesce(trade_count.c.trade_count, 0).desc())
        )
    else:
        stmt = stmt.order_by(Market.id)

    if max_markets is not None:
        stmt = stmt.limit(max_markets)

    return list(db.execute(stmt).scalars())


def reclassify_one(market: Market) -> None:
    """Run the layered classifier against a Market row in place.

    We call `classify(ticker=..., title=..., subtitle=...)` rather than
    passing a raw Kalshi market dict, because the dict isn't persisted —
    Layer 1 is therefore not used here, which is intentional. See module
    docstring.
    """
    result = classify(
        ticker=market.market_id,
        title=market.title,
        subtitle=market.subtitle,
    )
    market.category = result.category
    market.subcategory = result.subcategory
    market.manipulability_prior = result.manipulability_prior
    market.classifier_tags = list(result.tags)
    market.classifier_layer = result.layer
    market.classifier_rule = result.rule
    market.classifier_confidence = result.confidence
    market.classifier_version = CLASSIFIER_VERSION


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Cap on rows to reclassify this run. Default: no cap.",
    )
    parser.add_argument(
        "--top-by-trades",
        action="store_true",
        help="Reclassify high-trade-count markets first (dashboard-visible).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Reclassify every market, not just version-stale ones.",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=500,
        help="Commit every N rows. Default: 500.",
    )
    args = parser.parse_args()

    started = time.monotonic()
    db: Session = SessionLocal()
    try:
        targets = select_targets(
            db,
            top_by_trades=args.top_by_trades,
            max_markets=args.max,
            only_stale=not args.all,
        )
        print(
            f"classifier: backfill targets={len(targets)} "
            f"version={CLASSIFIER_VERSION} only_stale={not args.all} "
            f"top_by_trades={args.top_by_trades}"
        )

        layer_counts: dict[str, int] = {}
        confidence_counts: dict[str, int] = {}

        for i, market in enumerate(targets, start=1):
            reclassify_one(market)
            layer_counts[market.classifier_layer or "?"] = (
                layer_counts.get(market.classifier_layer or "?", 0) + 1
            )
            confidence_counts[market.classifier_confidence or "?"] = (
                confidence_counts.get(market.classifier_confidence or "?", 0) + 1
            )

            if i % args.batch == 0:
                db.commit()
                elapsed = time.monotonic() - started
                rate = i / elapsed if elapsed > 0 else 0.0
                print(f"  ... {i}/{len(targets)} ({rate:.1f}/s)")

        db.commit()

        elapsed = time.monotonic() - started
        rate = len(targets) / elapsed if elapsed > 0 else 0.0
        print(f"classifier: done {len(targets)} rows in {elapsed:.1f}s ({rate:.1f}/s)")
        print(f"classifier: by layer = {layer_counts}")
        print(f"classifier: by confidence = {confidence_counts}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
