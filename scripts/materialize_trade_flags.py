"""Score recent trades, persist durable flags, and promote evidence windows."""

from __future__ import annotations

import argparse
import json

from app.db.session import SessionLocal
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
)
from app.services.trade_flag_materializer import (
    TRADE_FLAG_MIN_SCORE,
    TRADE_SCORER_VERSION,
    materialize_trade_flags,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-trades", type=int, default=5000)
    parser.add_argument("--min-score", type=float, default=TRADE_FLAG_MIN_SCORE)
    parser.add_argument("--market-id", default=None)
    parser.add_argument("--scorer-version", type=int, default=TRADE_SCORER_VERSION)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_id = new_run_id("trade-flags")
    mark_pipeline_start(
        "trade_flags",
        detail="Starting standalone trade flag materializer.",
        run_id=run_id,
    )

    db = SessionLocal()
    try:
        result = materialize_trade_flags(
            db,
            max_trades=args.max_trades,
            min_score=args.min_score,
            scorer_version=args.scorer_version,
            market_id=args.market_id,
            dry_run=args.dry_run,
        )
        mark_pipeline_success(
            "trade_flags",
            detail=(
                f"Flagged {result.get('flagged', 0)} trades; "
                f"promoted {result.get('promoted', 0)}."
            ),
            run_id=run_id,
            count=int(result.get("created_or_updated") or 0),
            metadata=result,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        db.rollback()
        mark_pipeline_error(
            "trade_flags",
            exc,
            detail="Standalone trade flag materializer failed.",
            run_id=run_id,
        )
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
