"""Rebuild OpenSearch indexes for dashboard market/news search."""

from __future__ import annotations

import argparse
import logging

from app.db.session import SessionLocal
from app.services.search_index import (
    bulk_index_markets,
    bulk_index_news,
    ensure_search_indexes,
)

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--markets-limit", type=int, default=None)
    parser.add_argument("--news-limit", type=int, default=None)
    parser.add_argument("--skip-markets", action="store_true")
    parser.add_argument("--skip-news", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ensure_search_indexes()

    with SessionLocal() as db:
        market_count = 0
        news_count = 0
        if not args.skip_markets:
            logger.info("indexing markets")
            market_count = bulk_index_markets(
                db,
                batch_size=max(1, args.batch_size),
                limit=args.markets_limit,
            )
        if not args.skip_news:
            logger.info("indexing news articles")
            news_count = bulk_index_news(
                db,
                batch_size=max(1, args.batch_size),
                limit=args.news_limit,
            )

    logger.info(
        "search index rebuild complete: markets=%s news=%s",
        market_count,
        news_count,
    )


if __name__ == "__main__":
    main()
