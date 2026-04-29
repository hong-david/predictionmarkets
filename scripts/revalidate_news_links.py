"""Re-score stored news/market links with current relevance guardrails."""

from __future__ import annotations

import argparse
import json

from app.db.session import SessionLocal
from app.services.news_link_maintenance import revalidate_stored_news_links
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--min-relevance", type=float, default=0.35)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run_id = new_run_id("news-revalidate")
    mark_pipeline_start(
        "news_links",
        detail="Starting stored news-link revalidation.",
        run_id=run_id,
    )
    db = SessionLocal()
    try:
        result = revalidate_stored_news_links(
            db,
            limit=args.limit,
            min_relevance=args.min_relevance,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            db.commit()
        mark_pipeline_success(
            "news_links",
            detail=(
                f"Revalidated {result['processed']} links; "
                f"removed {result['removed']}."
            ),
            run_id=run_id,
            count=int(result["kept"]),
            metadata=result,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        db.rollback()
        mark_pipeline_error(
            "news_links",
            exc,
            detail="Stored news-link revalidation failed.",
            run_id=run_id,
        )
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
