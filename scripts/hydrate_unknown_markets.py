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
import re
import time
from datetime import date, datetime, timezone, timedelta

from sqlalchemy import func, or_

from app.db.models import Market, MarketMetric, Trade
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




_SCHEDULED_DATE_TOKEN_RE = re.compile(
    r"(\d{2}(?:JAN|MAR|MAY|JUL|AUG|OCT|DEC)(?:0[1-9]|[12][0-9]|3[01])"
    r"|\d{2}(?:APR|JUN|SEP|NOV)(?:0[1-9]|[12][0-9]|30)"
    r"|\d{2}FEB(?:0[1-9]|1[0-9]|2[0-9]))",
    re.IGNORECASE,
)

_MONTH_NUMBERS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def _market_id_has_non_stale_date(market_id: str, today: date) -> bool:
    """Return False for tickers with an embedded YYMONDD date before today.

    Tickers without an embedded date are kept because many series markets encode
    teams/rounds rather than a concrete event date.
    """
    match = _SCHEDULED_DATE_TOKEN_RE.search(market_id or "")
    if match is None:
        return True

    token = match.group(1).upper()
    try:
        token_date = date(
            2000 + int(token[:2]),
            _MONTH_NUMBERS[token[2:5]],
            int(token[5:]),
        )
    except (KeyError, ValueError):
        return True

    return token_date >= today


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

    We exclude obviously stale dated tickers in Python instead of SQL because
    PostgreSQL substring/regex capture behavior can return only a capture group.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
    today = datetime.now(timezone.utc).date()
    fetch_limit = max_markets * 20 if max_markets is not None else None

    db = SessionLocal()
    try:
        q = (
            db.query(Market.market_id)
            .outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
            .filter(Market.title != Market.market_id)
            .filter(Market.status.in_(("active", "open")))
            .filter(or_(Market.close_time.is_(None), Market.close_time > func.now()))
            .filter(
                or_(
                    func.coalesce(MarketMetric.latest_snapshot_ts, Market.updated_at).is_(None),
                    func.coalesce(MarketMetric.latest_snapshot_ts, Market.updated_at) < cutoff,
                )
            )
            .order_by(
                MarketMetric.trade_count.desc().nullslast(),
                Market.updated_at.asc().nullsfirst(),
                Market.id.desc(),
            )
        )

        if fetch_limit is not None:
            q = q.limit(fetch_limit)

        tickers: list[str] = []
        for row in q.all():
            ticker = row[0]
            if not _market_id_has_non_stale_date(ticker, today):
                continue

            tickers.append(ticker)
            if max_markets is not None and len(tickers) >= max_markets:
                break

        return tickers
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
