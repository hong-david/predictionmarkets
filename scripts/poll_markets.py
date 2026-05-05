"""Periodic REST poller: re-sweep the Kalshi market universe + materialize anomalies.

Why this looks the way it does
------------------------------
The old implementation called `KalshiRestClient.get_markets(limit=25)` once
per cycle. That had two problems:

1. **It only ever saw the first 25 markets** in Kalshi's alphabetical list,
   which the live smoke test taught us is the dead-exotic-combinatorial slice
   (`KXMVECROSSCATEGORY...` etc.), not the actively trading universe.
2. **It never hydrated the lazy-upsert backlog.** The WS consumer auto-creates
   stub `markets` rows with `status='unknown'` for tickers it sees but doesn't
   know yet (see `app.services.kalshi_ws._get_or_create_market`); the only
   writer that fills in title / event_ticker / open_time is this poller.
   Polling 25 alphabetically-first rows can never drain that queue.

This rewrite walks Kalshi's `cursor`-based pagination via `iter_markets` and
ingests in fixed-size batches so commits are progressive, not one giant
buffer. Each cycle does the full open-status sweep then re-runs the anomaly
materializer over the most-recent markets.

Defaults are tuned for "leave it running in the background":
- `status="open"` so we touch the actively trading set, not every settled
  market in history.
- `interval=300s` (5 min) so a full sweep + materialize comfortably finishes
  before the next cycle starts at typical Kalshi sizes (~50–60k open markets).
- `batch=500` matches the bootstrap script.

CLI flags exist mostly for ad-hoc runs from a developer machine — e.g.
`--max 500 --interval 30` to fast-iterate against a small slice.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from app.db.session import SessionLocal
from app.services.anomaly_materializer import materialize_anomalies
from app.services.kalshi_rest import KalshiRestClient
from app.services.market_ingestor import chunked, ingest_markets_payload
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
    record_pipeline_heartbeat,
)
from scripts.hydrate_unknown_markets import hydrate_unknown_tickers, refresh_active_lifecycle_tickers


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cycle(
    *,
    status: str | None,
    batch_size: int,
    max_markets: int | None,
    anomaly_market_limit: int,
    anomaly_lookback: int,
    hydrate_unknown_max: int,
    hydrate_unknown_sleep: float,
    refresh_active_lifecycle_max: int,
    refresh_active_lifecycle_min_age_minutes: int,
    refresh_active_lifecycle_sleep: float,
) -> dict[str, int]:
    """Single sweep over Kalshi /markets, batched ingest, then anomaly materialize.

    Returns the aggregated counts so the loop can log them. We keep the
    iteration logic here (rather than in `ingest_markets_payload`) so the
    ingestor stays a pure "given this payload, write these rows" function
    that integration tests can drive without touching the network.
    """
    print(f"[{_utc_now()}] cycle start status={status!r} batch={batch_size} max={max_markets}", flush=True)
    run_id = new_run_id("market-poller")
    mark_pipeline_start(
        "market_poller",
        detail="Starting market sweep.",
        run_id=run_id,
        metadata={"status_filter": status, "max_markets": max_markets},
    )

    client = KalshiRestClient()
    stream = client.iter_markets(status=status, max_markets=max_markets)

    totals = {"inserted_markets": 0, "updated_markets": 0, "snapshots_created": 0}
    batches = 0

    db = SessionLocal()
    try:
        for batch in chunked(stream, batch_size):
            batches += 1
            result = ingest_markets_payload(db, {"markets": batch})
            for k, v in result.items():
                totals[k] = totals.get(k, 0) + v
            print(
                f"[{_utc_now()}] batch {batches}: size={len(batch)} "
                f"inserted={result['inserted_markets']} "
                f"updated={result['updated_markets']} "
                f"snapshots={result['snapshots_created']} "
                f"running={totals}",
                flush=True,
            )
            record_pipeline_heartbeat(
                "market_poller",
                detail=f"Processed {batches} batches; latest batch size {len(batch)}.",
                run_id=run_id,
                count=totals.get("updated_markets", 0)
                + totals.get("inserted_markets", 0),
                metadata={"batches": batches, **totals},
            )

        anomaly_result = materialize_anomalies(
            db,
            market_limit=anomaly_market_limit,
            lookback=anomaly_lookback,
        )
        print(
            f"[{_utc_now()}] cycle done batches={batches} "
            f"ingest={totals} anomalies={anomaly_result}",
            flush=True,
        )
    finally:
        db.close()

    if hydrate_unknown_max > 0:
        hydrate_result = hydrate_unknown_tickers(
            top_by_trades=True,
            max_markets=hydrate_unknown_max,
            sleep_seconds=hydrate_unknown_sleep,
        )
        totals["hydrated_unknown"] = hydrate_result["hydrated"]
        totals["missing_unknown"] = hydrate_result["missing"]
        totals["errored_unknown"] = hydrate_result["errored"]

    if refresh_active_lifecycle_max > 0:
        lifecycle_result = refresh_active_lifecycle_tickers(
            max_markets=refresh_active_lifecycle_max,
            min_age_minutes=refresh_active_lifecycle_min_age_minutes,
            sleep_seconds=refresh_active_lifecycle_sleep,
        )
        totals["refreshed_active_lifecycle"] = lifecycle_result["refreshed"]
        totals["missing_active_lifecycle"] = lifecycle_result["missing"]
        totals["errored_active_lifecycle"] = lifecycle_result["errored"]

    mark_pipeline_success(
        "market_poller",
        detail=(
            f"Completed {batches} batches; inserted {totals.get('inserted_markets', 0)}, "
            f"updated {totals.get('updated_markets', 0)}, snapshots {totals.get('snapshots_created', 0)}."
        ),
        run_id=run_id,
        count=totals.get("updated_markets", 0) + totals.get("inserted_markets", 0),
        metadata={**totals, "batches": batches},
    )
    return totals


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
        help="Hard cap on markets per cycle. Default: no cap.",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=500,
        help="Markets per ingest commit. Default: 500.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Seconds between cycles. Default: 300 (5 min).",
    )
    parser.add_argument(
        "--anomaly-market-limit",
        type=int,
        default=100,
        help="Number of recent markets the anomaly materializer scans per cycle.",
    )
    parser.add_argument(
        "--anomaly-lookback",
        type=int,
        default=40,
        help="Snapshots per market the anomaly engine considers (rolling baselines need several).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit (useful for cron / CI / smoke tests).",
    )
    parser.add_argument(
        "--hydrate-unknown-max",
        type=int,
        default=250,
        help="Per cycle, hydrate this many status='unknown' tickers by per-market REST lookup. Use 0 to disable.",
    )
    parser.add_argument(
        "--hydrate-unknown-sleep",
        type=float,
        default=0.02,
        help="Seconds between per-ticker hydration calls. Default: 0.02.",
    )
    parser.add_argument(
        "--refresh-active-lifecycle-max",
        type=int,
        default=25,
        help="Per cycle, refresh this many stale local active/open markets by per-market REST lookup. Use 0 to disable.",
    )
    parser.add_argument(
        "--refresh-active-lifecycle-min-age-minutes",
        type=int,
        default=30,
        help="Only refresh local active/open markets whose updated_at is at least this old. Default: 30.",
    )
    parser.add_argument(
        "--refresh-active-lifecycle-sleep",
        type=float,
        default=0.02,
        help="Seconds between active lifecycle refresh calls. Default: 0.02.",
    )
    args = parser.parse_args()

    status = None if args.status == "all" else args.status

    print(
        f"poller starting: status={status!r} batch={args.batch} "
        f"interval={args.interval}s max={args.max} once={args.once}",
        flush=True,
    )

    try:
        while True:
            try:
                run_cycle(
                    status=status,
                    batch_size=args.batch,
                    max_markets=args.max,
                    anomaly_market_limit=args.anomaly_market_limit,
                    anomaly_lookback=args.anomaly_lookback,
                    hydrate_unknown_max=args.hydrate_unknown_max,
                    hydrate_unknown_sleep=args.hydrate_unknown_sleep,
                    refresh_active_lifecycle_max=args.refresh_active_lifecycle_max,
                    refresh_active_lifecycle_min_age_minutes=args.refresh_active_lifecycle_min_age_minutes,
                    refresh_active_lifecycle_sleep=args.refresh_active_lifecycle_sleep,
                )
            except Exception as exc:
                mark_pipeline_error("market_poller", exc, detail="Market sweep failed.")
                raise
            if args.once:
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Poller stopped.", flush=True)


if __name__ == "__main__":
    main()
