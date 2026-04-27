"""Score linked news events against pre-news market activity."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.db.session import SessionLocal
from app.services.news_trade_correlation import (
    NEWS_TRADE_MIN_RELEVANCE,
    materialize_news_trade_correlations,
    materialize_news_trade_correlations_async,
)


async def _watch(args: argparse.Namespace) -> None:
    while True:
        result = await materialize_news_trade_correlations_async(
            max_events=args.max_events,
            batch_size=args.batch_size,
            min_relevance=args.min_relevance,
            lookback_hours=args.lookback_hours,
            max_trades_per_event=args.max_trades_per_event,
            trade_flag_scorer_version=args.trade_flag_scorer_version,
            rescore=args.rescore,
            promote_cases=not args.no_promote_cases,
            dry_run=args.dry_run,
            sleep_seconds=args.batch_sleep_seconds,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        await asyncio.sleep(args.interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-events", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--min-relevance", type=float, default=NEWS_TRADE_MIN_RELEVANCE)
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument("--max-trades-per-event", type=int, default=1000)
    parser.add_argument("--trade-flag-scorer-version", type=int, default=None)
    parser.add_argument("--rescore", action="store_true")
    parser.add_argument("--no-promote-cases", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--async-run",
        action="store_true",
        help="Run the one-shot materializer through the async batch runner.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously run async sweeps until interrupted.",
    )
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--batch-sleep-seconds", type=float, default=0.0)
    args = parser.parse_args()

    if args.watch:
        asyncio.run(_watch(args))
        return

    if args.async_run:
        result = asyncio.run(
            materialize_news_trade_correlations_async(
                max_events=args.max_events,
                batch_size=args.batch_size,
                min_relevance=args.min_relevance,
                lookback_hours=args.lookback_hours,
                max_trades_per_event=args.max_trades_per_event,
                trade_flag_scorer_version=args.trade_flag_scorer_version,
                rescore=args.rescore,
                promote_cases=not args.no_promote_cases,
                dry_run=args.dry_run,
                sleep_seconds=args.batch_sleep_seconds,
            )
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    db = SessionLocal()
    try:
        result = materialize_news_trade_correlations(
            db,
            max_events=args.max_events,
            batch_size=args.batch_size,
            min_relevance=args.min_relevance,
            lookback_hours=args.lookback_hours,
            max_trades_per_event=args.max_trades_per_event,
            trade_flag_scorer_version=args.trade_flag_scorer_version,
            rescore=args.rescore,
            promote_cases=not args.no_promote_cases,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
