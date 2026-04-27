"""Score recent trades, persist durable flags, and promote evidence windows."""

from __future__ import annotations

import argparse
import json

from app.db.session import SessionLocal
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
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
