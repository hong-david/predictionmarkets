"""Review historical markets for suspicious-behavior signal quality.

This does not train a model. It gives a compact audit report for resolved or
past-close markets so we can inspect whether rule-based flags, pre-news links,
and quote/book anomalies fired before the market ended.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.db.models import (
    Anomaly,
    Market,
    MarketMetric,
    NewsArticle,
    NewsEvent,
    Trade,
    TradeFlag,
)
from app.db.session import SessionLocal

_ACTIVE_STATUSES = {"open", "active"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _historical_filter(now: datetime):
    status = func.lower(func.coalesce(Market.status, ""))
    return or_(status.notin_(tuple(_ACTIVE_STATUSES)), Market.close_time <= now)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _flag_summary(
    db: Session,
    market: Market,
    *,
    min_flag_score: float,
) -> dict[str, Any]:
    q = db.query(TradeFlag).filter(TradeFlag.market_pk == market.id)
    if min_flag_score > 0:
        q = q.filter(TradeFlag.score >= min_flag_score)
    all_rows = q.order_by(TradeFlag.score.desc(), TradeFlag.ts.asc()).limit(5).all()
    before_close = [
        row
        for row in all_rows
        if market.close_time is None or row.ts <= market.close_time
    ]
    best = all_rows[0] if all_rows else None
    return {
        "count_sample": len(all_rows),
        "before_close_sample": len(before_close),
        "best_score": float(best.score) if best is not None else 0.0,
        "best_severity": best.severity if best is not None else None,
        "best_ts": _iso(best.ts) if best is not None else None,
        "best_reasons": best.reasons if best is not None else [],
    }


def _news_summary(db: Session, market: Market) -> dict[str, Any]:
    rows = (
        db.query(NewsEvent, NewsArticle)
        .join(NewsArticle, NewsArticle.id == NewsEvent.article_id)
        .filter(NewsEvent.market_pk == market.id)
        .filter(NewsEvent.pre_news_trade_score > 0)
        .order_by(NewsEvent.pre_news_trade_score.desc(), NewsArticle.first_seen_at.asc())
        .limit(5)
        .all()
    )
    before_close = [
        (event, article)
        for event, article in rows
        if market.close_time is None or article.first_seen_at <= market.close_time
    ]
    best = rows[0] if rows else None
    best_event, best_article = best if best is not None else (None, None)
    return {
        "count_sample": len(rows),
        "before_close_sample": len(before_close),
        "best_score": float(best_event.pre_news_trade_score)
        if best_event is not None
        else 0.0,
        "best_article": best_article.title if best_article is not None else None,
        "best_source": best_article.domain if best_article is not None else None,
        "first_seen_at": _iso(best_article.first_seen_at)
        if best_article is not None
        else None,
        "leakage_window_seconds": best_event.leakage_window_seconds
        if best_event is not None
        else None,
    }


def _anomaly_summary(db: Session, market: Market) -> dict[str, Any]:
    high_case = case((Anomaly.severity.in_(("high", "critical")), 1), else_=0)
    count, high_count, last_ts = (
        db.query(
            func.count(Anomaly.id),
            func.sum(high_case),
            func.max(Anomaly.created_at),
        )
        .filter(Anomaly.market_pk == market.id)
        .one()
    )
    return {
        "count": int(count or 0),
        "high_count": int(high_count or 0),
        "last_ts": _iso(last_ts),
    }


def _label_case(flags: dict[str, Any], news: dict[str, Any], anomalies: dict[str, Any]) -> str:
    if flags["best_score"] >= 8 and news["best_score"] > 0:
        return "strong_pre_news_review"
    if flags["best_score"] >= 8:
        return "strong_trade_review"
    if news["best_score"] > 0:
        return "pre_news_review"
    if anomalies["high_count"] > 0:
        return "quote_book_review"
    if flags["best_score"] >= 5 or anomalies["count"] > 0:
        return "weak_review"
    return "quiet"


def historical_signal_report(
    db: Session,
    *,
    limit: int = 25,
    min_flag_score: float = 0.0,
    category: str | None = None,
    market_id: str | None = None,
) -> dict[str, Any]:
    now = _now()
    q = (
        db.query(Market, MarketMetric)
        .outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
        .filter(_historical_filter(now))
        .filter(Market.status != "unknown")
        .filter(Market.title != Market.market_id)
    )
    if category:
        q = q.filter(Market.category == category)
    if market_id:
        q = q.filter(Market.market_id == market_id)
    rows = (
        q.order_by(
            MarketMetric.urgency_score.desc().nullslast(),
            Market.close_time.desc().nullslast(),
            Market.id.desc(),
        )
        .limit(limit)
        .all()
    )
    markets = []
    for market, metric in rows:
        flags = _flag_summary(db, market, min_flag_score=min_flag_score)
        news = _news_summary(db, market)
        anomalies = _anomaly_summary(db, market)
        trade_count = int(metric.trade_count or 0) if metric else 0
        if trade_count == 0:
            trade_count = int(
                db.query(func.count(Trade.id))
                .filter(Trade.market_pk == market.id)
                .scalar()
                or 0
            )
        markets.append(
            {
                "market_id": market.market_id,
                "title": market.title,
                "status": market.status,
                "category": market.category,
                "prior": market.manipulability_prior,
                "close_time": _iso(market.close_time),
                "trade_count": trade_count,
                "metric_urgency_score": float(metric.urgency_score or 0.0)
                if metric
                else 0.0,
                "flags": flags,
                "pre_news": news,
                "quote_book_anomalies": anomalies,
                "qa_label": _label_case(flags, news, anomalies),
            }
        )
    return {
        "generated_at": now.isoformat(),
        "limit": limit,
        "min_flag_score": min_flag_score,
        "category": category,
        "market_id": market_id,
        "markets": markets,
    }


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
