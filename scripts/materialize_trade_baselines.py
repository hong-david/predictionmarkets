"""Precompute category/subcategory trade baselines for contextual scoring."""

from __future__ import annotations

import argparse

from app.db.session import SessionLocal
from app.services.trade_baselines import materialize_trade_baselines
from app.services.trade_flag_materializer import TRADE_SCORER_VERSION


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-hours", type=int, default=24 * 14)
    parser.add_argument("--max-trades", type=int, default=100_000)
    parser.add_argument("--min-points", type=int, default=25)
    parser.add_argument("--scorer-version", type=int, default=TRADE_SCORER_VERSION)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        n = materialize_trade_baselines(
            db,
            lookback_hours=args.lookback_hours,
            max_trades=args.max_trades,
            min_points=args.min_points,
            scorer_version=args.scorer_version,
        )
        db.commit()
        print(
            "trade_baselines: upserted "
            f"{n} rows (lookback={args.lookback_hours}h, "
            f"max_trades={args.max_trades}, min_points={args.min_points})"
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
