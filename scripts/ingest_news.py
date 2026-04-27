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
    parser.add_argument("--max-markets", type=int, default=5000)
    parser.add_argument("--max-profiles", type=int, default=5000)
    parser.add_argument("--min-relevance", type=float, default=0.35)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = ingest_global_news(
            db,
            query=args.query,
            lookback_hours=args.lookback_hours,
            limit=args.limit,
            max_markets=args.max_markets,
            max_profiles=args.max_profiles,
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
