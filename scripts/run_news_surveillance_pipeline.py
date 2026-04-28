"""Run one news surveillance sweep: ingest, flag trades, correlate news."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import time

from app.db.session import SessionLocal
from app.services.news_ingestor import DEFAULT_GLOBAL_NEWS_QUERY, ingest_global_news
from app.services.news_trade_correlation import materialize_news_trade_correlations
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
    record_pipeline_heartbeat,
)
from app.services.trade_flag_materializer import (
    TRADE_FLAG_MIN_SCORE,
    TRADE_SCORER_VERSION,
    materialize_trade_flags,
)


def run_cycle(args: argparse.Namespace) -> dict[str, object]:
    out: dict[str, object] = {}
    run_id = new_run_id("news-pipeline")
    db = SessionLocal()
    try:
        mark_pipeline_start(
            "news_ingest",
            detail="Starting global news ingest.",
            run_id=run_id,
        )
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
        mark_pipeline_success(
            "news_ingest",
            detail=(
                f"Fetched {news_result.get('articles_seen', 0)} articles; "
                f"linked {news_result.get('news_events_linked', 0)} events."
            ),
            run_id=run_id,
            count=int(news_result.get("articles_upserted") or 0),
            metadata=news_result,
        )
        mark_pipeline_success(
            "news_links",
            detail=f"Linked {news_result.get('news_events_linked', 0)} news events.",
            run_id=run_id,
            count=int(news_result.get("news_events_linked") or 0),
            metadata=news_result,
        )
    except Exception as exc:
        db.rollback()
        mark_pipeline_error(
            "news_ingest",
            exc,
            detail="News ingest failed.",
            run_id=run_id,
        )
        raise
    finally:
        db.close()

    if not args.skip_trade_flags:
        db = SessionLocal()
        try:
            mark_pipeline_start(
                "trade_flags",
                detail="Starting trade flag materializer.",
                run_id=run_id,
            )
            out["trade_flags"] = materialize_trade_flags(
                db,
                max_trades=args.max_trades,
                min_score=args.trade_min_score,
                scorer_version=args.trade_scorer_version,
                dry_run=args.dry_run,
            )
            trade_result = out["trade_flags"]
            mark_pipeline_success(
                "trade_flags",
                detail=(
                    f"Flagged {trade_result.get('flagged', 0)} trades; "
                    f"promoted {trade_result.get('promoted', 0)}."
                ),
                run_id=run_id,
                count=int(trade_result.get("created_or_updated") or 0),
                metadata=trade_result,
            )
        except Exception as exc:
            mark_pipeline_error(
                "trade_flags",
                exc,
                detail="Trade flag materializer failed.",
                run_id=run_id,
            )
            raise
        finally:
            db.close()
    else:
        record_pipeline_heartbeat(
            "trade_flags",
            detail="Skipped by news surveillance pipeline args.",
            run_id=run_id,
        )

    db = SessionLocal()
    try:
        mark_pipeline_start(
            "news_trade_correlations",
            detail="Starting news/trade correlation materializer.",
            run_id=run_id,
        )
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
        corr_result = out["news_trade_correlations"]
        mark_pipeline_success(
            "news_trade_correlations",
            detail=(
                f"Processed {corr_result.get('processed', 0)} events; "
                f"promoted {corr_result.get('promoted', 0)}."
            ),
            run_id=run_id,
            count=int(corr_result.get("updated") or 0),
            metadata=corr_result,
        )
    except Exception as exc:
        mark_pipeline_error(
            "news_trade_correlations",
            exc,
            detail="News/trade correlation materializer failed.",
            run_id=run_id,
        )
        raise
    finally:
        db.close()

    return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default=DEFAULT_GLOBAL_NEWS_QUERY)
    parser.add_argument("--lookback-hours", type=int, default=168)
    parser.add_argument("--news-limit", type=int, default=500)
    parser.add_argument("--rss-feed", action="append", default=None)
    parser.add_argument("--no-gdelt", action="store_true")
    parser.add_argument("--limit-per-rss-feed", type=int, default=150)
    parser.add_argument("--max-markets", type=int, default=12000)
    parser.add_argument("--max-profiles", type=int, default=12000)
    parser.add_argument("--max-candidates-per-article", type=int, default=150)
    parser.add_argument("--min-relevance", type=float, default=0.35)
    parser.add_argument("--skip-trade-flags", action="store_true")
    parser.add_argument("--max-trades", type=int, default=5000)
    parser.add_argument("--trade-min-score", type=float, default=TRADE_FLAG_MIN_SCORE)
    parser.add_argument("--trade-scorer-version", type=int, default=TRADE_SCORER_VERSION)
    parser.add_argument("--max-news-events", type=int, default=2000)
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
            record_pipeline_heartbeat(
                "news_ingest",
                detail="News surveillance watch loop alive.",
            )
            print(json.dumps(run_cycle(args), indent=2, sort_keys=True), flush=True)
            time.sleep(args.interval_seconds)

    print(json.dumps(run_cycle(args), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
