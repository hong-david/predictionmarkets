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
from datetime import datetime, timezone, timedelta

from sqlalchemy import func, or_

from app.db.models import Market, MarketMetric, Trade
from app.db.session import SessionLocal
from app.services.kalshi_rest import KalshiRestClient
from app.services.market_ingestor import ingest_markets_payload
from app.services.market_lifecycle import _market_scope_filters


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


def hydrate_unknown_tickers(
    *,
    top_by_trades: bool,
    max_markets: int | None,
    sleep_seconds: float,
) -> dict[str, int]:
    tickers = select_unknown_tickers(
        top_by_trades=top_by_trades, max_markets=max_markets
    )
    if not tickers:
        print(f"[{_utc_now()}] no markets with status='unknown'; nothing to do")
        return {"hydrated": 0, "missing": 0, "errored": 0}

    print(
        f"[{_utc_now()}] hydrating {len(tickers)} unknown ticker(s) "
        f"(top_by_trades={top_by_trades}, sleep={sleep_seconds}s)",
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
                time.sleep(sleep_seconds)
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

            time.sleep(sleep_seconds)
    finally:
        db.close()

    print(
        f"[{_utc_now()}] done. hydrated={hydrated} missing={missing} errored={errored}",
        flush=True,
    )
    return {"hydrated": hydrated, "missing": missing, "errored": errored}



def select_active_lifecycle_refresh_tickers(
    *,
    max_markets: int | None,
    min_age_minutes: int,
) -> list[str]:
    """Return local active/open markets whose lifecycle metadata is stale.

    The normal poller sweeps Kalshi with status=open. Once a market finalizes
    upstream, it can disappear from that sweep while the local row still says
    active/open. This bounded selector finds high-impact local active rows that
    have not been touched recently and lets per-market REST correct status and
    close_time through the normal ingestor path.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)

    db = SessionLocal()
    try:
        q = (
            db.query(Market.market_id)
            .outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
            .filter(Market.title != Market.market_id)
            .filter(*_market_scope_filters("active"))
            .filter(or_(Market.updated_at.is_(None), Market.updated_at < cutoff))
            .order_by(
                MarketMetric.trade_count.desc().nullslast(),
                Market.updated_at.asc().nullsfirst(),
                Market.id.desc(),
            )
        )
        if max_markets is not None:
            q = q.limit(max_markets)
        return [row[0] for row in q.all()]
    finally:
        db.close()


def refresh_active_lifecycle_tickers(
    *,
    max_markets: int | None,
    min_age_minutes: int,
    sleep_seconds: float,
) -> dict[str, int]:
    """Refresh a bounded set of local active/open markets by per-market REST."""
    tickers = select_active_lifecycle_refresh_tickers(
        max_markets=max_markets,
        min_age_minutes=min_age_minutes,
    )

    if not tickers:
        print(
            f"[{_utc_now()}] no stale active/open markets need lifecycle refresh",
            flush=True,
        )
        return {"refreshed": 0, "missing": 0, "errored": 0}

    print(
        f"[{_utc_now()}] refreshing lifecycle for {len(tickers)} active/open ticker(s) "
        f"(min_age={min_age_minutes}m, sleep={sleep_seconds}s)",
        flush=True,
    )

    client = KalshiRestClient()
    refreshed = 0
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
                time.sleep(sleep_seconds)
                continue

            if market is None:
                missing += 1
                print(f"  {ticker}: missing upstream", flush=True)
            else:
                ingest_markets_payload(db, {"markets": [market]})
                refreshed += 1

            if i % 25 == 0 or i == len(tickers):
                print(
                    f"  [{_utc_now()}] lifecycle progress {i}/{len(tickers)} "
                    f"refreshed={refreshed} missing={missing} errored={errored}",
                    flush=True,
                )

            time.sleep(sleep_seconds)
    finally:
        db.close()

    print(
        f"[{_utc_now()}] lifecycle refresh done. "
        f"refreshed={refreshed} missing={missing} errored={errored}",
        flush=True,
    )
    return {"refreshed": refreshed, "missing": missing, "errored": errored}


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

    hydrate_unknown_tickers(
        top_by_trades=args.top_by_trades,
        max_markets=args.max,
        sleep_seconds=args.sleep,
    )


if __name__ == "__main__":
    main()
