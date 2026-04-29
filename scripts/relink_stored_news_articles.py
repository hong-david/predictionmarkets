"""Re-run current article-to-market linking over stored news articles."""

from __future__ import annotations

import argparse
import json

from app.db.session import SessionLocal
from app.services.news_link_maintenance import relink_stored_articles
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--article-limit", type=int, default=5000)
    parser.add_argument("--max-markets", type=int, default=12000)
    parser.add_argument("--max-profiles", type=int, default=12000)
    parser.add_argument("--max-candidates-per-article", type=int, default=150)
    parser.add_argument("--max-factor-profiles-per-category", type=int, default=5000)
    parser.add_argument("--min-relevance", type=float, default=0.35)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_id = new_run_id("news-relink")
    mark_pipeline_start(
        "news_links",
        detail="Starting stored article relink.",
        run_id=run_id,
    )
    db = SessionLocal()
    try:
        result = relink_stored_articles(
            db,
            article_limit=args.article_limit,
            max_markets=args.max_markets,
            max_profiles=args.max_profiles,
            max_candidates_per_article=args.max_candidates_per_article,
            max_factor_profiles_per_category=args.max_factor_profiles_per_category,
            min_relevance=args.min_relevance,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            db.commit()
        mark_pipeline_success(
            "news_links",
            detail=(
                f"Relinked {result['articles_processed']} stored articles; "
                f"updated {result['links_created_or_updated']} links."
            ),
            run_id=run_id,
            count=int(result["links_created_or_updated"]),
            metadata=result,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        db.rollback()
        mark_pipeline_error(
            "news_links",
            exc,
            detail="Stored article relink failed.",
            run_id=run_id,
        )
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
