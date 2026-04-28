"""Review historical markets for suspicious-behavior signal quality.

This does not train a model. It gives a compact audit report for resolved or
past-close markets so we can inspect whether rule-based flags, pre-news links,
and quote/book anomalies fired before the market ended.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.db.session import SessionLocal
from app.services.historical_signal_qa import historical_signal_report


def _print_markdown(report: dict[str, Any]) -> None:
    print(f"# Historical Signal QA ({report['generated_at']})")
    print()
    for row in report["markets"]:
        print(f"## {row['qa_label']} - {row['market_id']}")
        print(row["title"])
        print(
            f"- status: {row['status']} | close: {row['close_time']} | "
            f"category: {row['category']} | prior: {row['prior']}"
        )
        print(
            f"- trades: {row['trade_count']} | flags best: "
            f"{row['flags']['best_score']:.2f} | pre-news best: "
            f"{row['pre_news']['best_score']:.2f} | anomalies: "
            f"{row['quote_book_anomalies']['count']}"
        )
        if row["pre_news"]["best_article"]:
            print(f"- article: {row['pre_news']['best_article']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--min-flag-score", type=float, default=0.0)
    parser.add_argument("--category", default=None)
    parser.add_argument("--market-id", default=None)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        report = historical_signal_report(
            db,
            limit=args.limit,
            min_flag_score=args.min_flag_score,
            category=args.category,
            market_id=args.market_id,
        )
    finally:
        db.close()
    if args.format == "markdown":
        _print_markdown(report)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
