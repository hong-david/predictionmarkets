"""Run one news surveillance sweep: ingest, flag trades, correlate news."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import time

from app.db.session import SessionLocal
from app.services.news_ingestor import DEFAULT_GLOBAL_NEWS_QUERY, ingest_global_news
from app.services.news_trade_correlation import materialize_news_trade_correlations
from app.services.trade_flag_materializer import (
    TRADE_FLAG_MIN_SCORE,
    TRADE_SCORER_VERSION,
    materialize_trade_flags,
)


def run_cycle(args: argparse.Namespace) -> dict[str, object]:
    out: dict[str, object] = {}
    db = SessionLocal()
    try:
        news_result = ingest_global_news(
            db,
            query=args.query,
            lookback_hours=args.lookback_hours,
            limit=args.news_limit,
            max_markets=args.max_markets,
            max_profiles=args.max_profiles,
            min_relevance=args.min_relevance,
            include_gdelt=not args.no_gdelt,
            rss_feeds=args.rss_feed,
            limit_per_rss_feed=args.limit_per_rss_feed,
            max_candidates_per_article=args.max_candidates_per_article,
        )
        if not args.dry_run:
            db.commit()
        out["news_ingest"] = news_result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    if not args.skip_trade_flags:
        db = SessionLocal()
        try:
            out["trade_flags"] = materialize_trade_flags(
                db,
                max_trades=args.max_trades,
                min_score=args.trade_min_score,
                scorer_version=args.trade_scorer_version,
                dry_run=args.dry_run,
            )
        finally:
            db.close()

    db = SessionLocal()
    try:
        out["news_trade_correlations"] = materialize_news_trade_correlations(
            db,
            max_events=args.max_news_events,
            batch_size=args.batch_size,
            min_relevance=args.min_relevance,
            lookback_hours=args.correlation_lookback_hours,
            max_trades_per_event=args.max_trades_per_event,
            trade_flag_scorer_version=args.trade_scorer_version,
            rescore=args.rescore_news,
            promote_cases=not args.no_promote_cases,
            dry_run=args.dry_run,
        )
    finally:
        db.close()

    return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default=DEFAULT_GLOBAL_NEWS_QUERY)
    parser.add_argument("--lookback-hours", type=int, default=6)
    parser.add_argument("--news-limit", type=int, default=100)
    parser.add_argument("--rss-feed", action="append", default=None)
    parser.add_argument("--no-gdelt", action="store_true")
    parser.add_argument("--limit-per-rss-feed", type=int, default=50)
    parser.add_argument("--max-markets", type=int, default=5000)
    parser.add_argument("--max-profiles", type=int, default=5000)
    parser.add_argument("--max-candidates-per-article", type=int, default=100)
    parser.add_argument("--min-relevance", type=float, default=0.35)
    parser.add_argument("--skip-trade-flags", action="store_true")
    parser.add_argument("--max-trades", type=int, default=5000)
    parser.add_argument("--trade-min-score", type=float, default=TRADE_FLAG_MIN_SCORE)
    parser.add_argument("--trade-scorer-version", type=int, default=TRADE_SCORER_VERSION)
    parser.add_argument("--max-news-events", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--correlation-lookback-hours", type=float, default=24.0)
    parser.add_argument("--max-trades-per-event", type=int, default=1000)
    parser.add_argument("--rescore-news", action="store_true")
    parser.add_argument("--no-promote-cases", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    args = parser.parse_args()

    if args.watch:
        print(
            f"[{_utc_now()}] news surveillance pipeline starting "
            f"interval={args.interval_seconds}s",
            flush=True,
        )
        while True:
            print(json.dumps(run_cycle(args), indent=2, sort_keys=True), flush=True)
            time.sleep(args.interval_seconds)

    print(json.dumps(run_cycle(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
