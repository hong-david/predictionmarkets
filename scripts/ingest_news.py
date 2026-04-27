"""Ingest global news articles and link them to market profiles."""

from __future__ import annotations

import argparse
import json

from app.db.session import SessionLocal
from app.services.news_ingestor import DEFAULT_GLOBAL_NEWS_QUERY, ingest_global_news


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default=DEFAULT_GLOBAL_NEWS_QUERY)
    parser.add_argument("--lookback-hours", type=int, default=6)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--rss-feed",
        action="append",
        default=None,
        help="RSS/Atom feed URL to ingest. May be passed more than once.",
    )
    parser.add_argument(
        "--no-gdelt",
        action="store_true",
        help="Disable the default GDELT fetch and ingest only configured RSS feeds.",
    )
    parser.add_argument("--limit-per-rss-feed", type=int, default=50)
    parser.add_argument("--max-markets", type=int, default=5000)
    parser.add_argument("--max-profiles", type=int, default=5000)
    parser.add_argument("--max-candidates-per-article", type=int, default=None)
    parser.add_argument("--min-relevance", type=float, default=0.35)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = ingest_global_news(
            db,
            query=args.query,
            lookback_hours=args.lookback_hours,
            limit=args.limit,
            include_gdelt=not args.no_gdelt,
            rss_feeds=args.rss_feed,
            limit_per_rss_feed=args.limit_per_rss_feed,
            max_markets=args.max_markets,
            max_profiles=args.max_profiles,
            max_candidates_per_article=args.max_candidates_per_article,
            min_relevance=args.min_relevance,
        )
        db.commit()
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
