"""One-off backfill of the open Kalshi market universe.

The previous implementation fetched a single page of 25 markets, which was
how we ended up with a tiny, non-representative slice of the universe and a
WS smoke test that matched zero live tickers. This version walks Kalshi's
cursor-based pagination via `KalshiRestClient.iter_markets` and ingests in
batches so we get periodic progress instead of one giant blocking call.

Usage:
    python -m scripts.bootstrap_markets                # all open markets
    python -m scripts.bootstrap_markets --status all   # everything
    python -m scripts.bootstrap_markets --max 500      # cap (useful for dev)
    python -m scripts.bootstrap_markets --batch 250    # batch size
"""

from __future__ import annotations

import argparse

from app.db.session import SessionLocal
from app.services.kalshi_rest import KalshiRestClient
from app.services.market_ingestor import chunked, ingest_markets_payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--status",
        default="open",
        help="Kalshi market status filter, or 'all' to disable. Default: open.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Hard cap on markets to ingest (useful for dev). Default: no cap.",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=500,
        help="Markets per ingest batch. Default: 500.",
    )
    args = parser.parse_args()

    status = None if args.status == "all" else args.status

    client = KalshiRestClient()
    stream = client.iter_markets(status=status, max_markets=args.max)

    total = {"inserted_markets": 0, "updated_markets": 0, "snapshots_created": 0}
    batches = 0

    db = SessionLocal()
    try:
        for batch in chunked(stream, args.batch):
            batches += 1
            result = ingest_markets_payload(db, {"markets": batch})
            for k, v in result.items():
                total[k] = total.get(k, 0) + v
            print(
                f"batch {batches}: size={len(batch)} "
                f"inserted={result['inserted_markets']} "
                f"updated={result['updated_markets']} "
                f"snapshots={result['snapshots_created']} "
                f"running_total={total}",
                flush=True,
            )
    finally:
        db.close()

    print(f"done. batches={batches} totals={total}", flush=True)


if __name__ == "__main__":
    main()
