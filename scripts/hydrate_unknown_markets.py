"""Backfill metadata for markets discovered via the WS lazy-upsert path.

The WS consumer auto-creates stub `Market` rows with `status='unknown'` for
tickers it sees on `ticker` / `trade` channels but doesn't yet know about.
The bulk REST poller hydrates many of them on its 5-minute sweep, but it's
filtered by `status=open` and so cannot reach markets that are currently
`active` (live) or `finalized` (just expired) — which on Kalshi are exactly
the high-trade-count, currently-trading markets we most want to display.

Solution: hit `/markets/{ticker}` per ticker for the unknown backlog. Way
cheaper than re-sweeping the universe, and the right shape for a "drain
the backlog the WS feed produced" task. Reuses `ingest_markets_payload`
so the upsert path is identical to the bulk poller.

Usage:
    python -m scripts.hydrate_unknown_markets                 # all unknowns
    python -m scripts.hydrate_unknown_markets --max 200       # cap
    python -m scripts.hydrate_unknown_markets --top-by-trades # prioritize active ones
    python -m scripts.hydrate_unknown_markets --sleep 0.05    # throttle
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from sqlalchemy import func

from app.db.models import Market, Trade
from app.db.session import SessionLocal
from app.services.kalshi_rest import KalshiRestClient
from app.services.market_ingestor import ingest_markets_payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_unknown_tickers(
    *, top_by_trades: bool, max_markets: int | None
) -> list[str]:
    """Return the lazy-upsert backlog, optionally ordered by trade activity.

    `top_by_trades` is the one most worth hydrating first because those are
    the markets the dashboard surfaces; an unhydrated row at the top is the
    actual UX paper-cut this script exists to fix.
    """
    db = SessionLocal()
    try:
        q = db.query(Market.market_id).filter(Market.status == "unknown")
        if top_by_trades:
            q = (
                db.query(Market.market_id)
                .outerjoin(Trade, Trade.market_pk == Market.id)
                .filter(Market.status == "unknown")
                .group_by(Market.id)
                .order_by(func.count(Trade.id).desc(), Market.id.desc())
            )
        if max_markets is not None:
            q = q.limit(max_markets)
        return [row[0] for row in q.all()]
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Cap on tickers to hydrate this run. Default: no cap.",
    )
    parser.add_argument(
        "--top-by-trades",
        action="store_true",
        help="Order unknown markets by descending trade_count. Recommended.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.05,
        help="Seconds to sleep between Kalshi calls. Default: 0.05 (~20 req/s).",
    )
    args = parser.parse_args()

    tickers = select_unknown_tickers(
        top_by_trades=args.top_by_trades, max_markets=args.max
    )
    if not tickers:
        print(f"[{_utc_now()}] no markets with status='unknown'; nothing to do")
        return

    print(
        f"[{_utc_now()}] hydrating {len(tickers)} unknown ticker(s) "
        f"(top_by_trades={args.top_by_trades}, sleep={args.sleep}s)",
        flush=True,
    )

    client = KalshiRestClient()
    hydrated = 0
    missing = 0
    errored = 0

    db = SessionLocal()
    try:
        for i, ticker in enumerate(tickers, start=1):
            try:
                market = client.get_market(ticker)
            except Exception as e:
                errored += 1
                print(f"  {ticker}: error {e!r}", flush=True)
                time.sleep(args.sleep)
                continue

            if market is None:
                missing += 1
            else:
                ingest_markets_payload(db, {"markets": [market]})
                hydrated += 1

            if i % 50 == 0 or i == len(tickers):
                print(
                    f"  [{_utc_now()}] progress {i}/{len(tickers)} "
                    f"hydrated={hydrated} missing={missing} errored={errored}",
                    flush=True,
                )

            time.sleep(args.sleep)
    finally:
        db.close()

    print(
        f"[{_utc_now()}] done. hydrated={hydrated} missing={missing} errored={errored}",
        flush=True,
    )


if __name__ == "__main__":
    main()
