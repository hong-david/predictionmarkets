from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Market, NewsArticle, NewsEvent, Trade
from app.services.news_trade_correlation import (
    materialize_news_trade_correlations_async,
    score_news_trade_correlation,
)


def _ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc)


def _article() -> NewsArticle:
    return NewsArticle(
        id=1,
        canonical_url_hash="article",
        canonical_url="https://example.com/article",
        title="Trump appoints pro digital currency regulator to lead SEC",
        first_seen_at=_ts(12),
    )


def _event(label: str = "supports_yes") -> NewsEvent:
    return NewsEvent(
        id=1,
        article_id=1,
        market_pk=1,
        relevance_score=0.9,
        status="candidate",
        score_components={
            "market_direction": {
                "label": label,
                "confidence": 0.65,
                "market_orientation": "above_threshold",
                "underlier_direction": "bullish_underlier",
            }
        },
    )


def _trade(
    trade_id: str,
    ts: datetime,
    price: float,
    count: float,
    side: str = "yes",
    *,
    pk: int,
) -> Trade:
    return Trade(
        id=pk,
        market_pk=1,
        trade_id=trade_id,
        ts=ts,
        yes_price_dollars=Decimal(str(price)),
        no_price_dollars=Decimal(str(1 - price)),
        count_fp=Decimal(str(count)),
        taker_side=side,
    )


def test_aligned_pre_news_trade_scores_signal() -> None:
    trades = [
        _trade("t1", _ts(11, 30), 0.48, 10, pk=1),
        _trade("t2", _ts(11, 50), 0.53, 250, pk=2),
        _trade("t3", _ts(11, 56), 0.61, 20, pk=3),
    ]

    result = score_news_trade_correlation(
        article=_article(),
        event=_event("supports_yes"),
        trades=trades,
    )

    assert result.score >= 4.0
    assert result.status == "pre_news_signal"
    assert result.best_trade_id == "t2"
    assert result.leakage_window_seconds == 600
    assert "direction_aligned_with_news" in result.reasons
    assert "price_moved_toward_news_side" in result.reasons


def test_opposite_direction_trade_is_not_overflagged() -> None:
    trades = [
        _trade("t1", _ts(11, 30), 0.48, 10, pk=1),
        _trade("t2", _ts(11, 50), 0.53, 250, pk=2),
        _trade("t3", _ts(11, 56), 0.61, 20, pk=3),
    ]

    result = score_news_trade_correlation(
        article=_article(),
        event=_event("supports_no"),
        trades=trades,
    )

    assert result.score < 4.0
    assert result.status == "no_pre_news_signal"
    assert "opposite_to_news_direction" in result.reasons


def test_post_news_trade_is_ignored() -> None:
    result = score_news_trade_correlation(
        article=_article(),
        event=_event("supports_yes"),
        trades=[_trade("late", _ts(12, 1), 0.65, 500, pk=1)],
    )

    assert result.score == 0.0
    assert result.status == "no_pre_news_signal"
    assert result.reasons == ("no_pre_news_trades",)


def test_ambiguous_news_needs_more_than_volume() -> None:
    trades = [
        _trade("t1", _ts(11, 40), 0.50, 10, pk=1),
        _trade("t2", _ts(11, 50), 0.50, 5000, pk=2),
    ]

    result = score_news_trade_correlation(
        article=_article(),
        event=_event("ambiguous"),
        trades=trades,
    )

    assert result.score < 4.0
    assert result.status == "no_pre_news_signal"
    assert "direction_aligned_with_news" not in result.reasons


def _session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _seed_event(db, *, event_id: int, market_id: str, article_hash: str) -> None:
    market = Market(
        id=event_id,
        platform="kalshi",
        market_id=market_id,
        title="Will Bitcoin trade above 100000 by year end?",
        status="open",
        category="crypto_strike",
    )
    article = NewsArticle(
        id=event_id,
        canonical_url_hash=article_hash,
        canonical_url=f"https://example.com/{article_hash}",
        title="Trump appoints pro digital currency regulator to lead SEC",
        first_seen_at=_ts(12),
    )
    event = NewsEvent(
        id=event_id,
        article_id=event_id,
        market_pk=event_id,
        relevance_score=0.9,
        status="candidate",
        score_components={
            "market_direction": {
                "label": "supports_yes",
                "confidence": 0.65,
            }
        },
    )
    db.add_all([market, article, event])
    db.flush()
    for offset, price, count in ((30, 0.48, 10), (10, 0.53, 250), (4, 0.61, 20)):
        db.add(
            Trade(
                market_pk=event_id,
                trade_id=f"{market_id}-{offset}",
                ts=_ts(12) - timedelta(minutes=offset),
                yes_price_dollars=Decimal(str(price)),
                no_price_dollars=Decimal(str(1 - price)),
                count_fp=Decimal(str(count)),
                taker_side="yes",
            )
        )


def test_async_materializer_processes_batches_and_updates_events() -> None:
    Session = _session_factory()
    db = Session()
    try:
        _seed_event(db, event_id=1, market_id="KXBTC-1", article_hash="a1")
        _seed_event(db, event_id=2, market_id="KXBTC-2", article_hash="a2")
        db.commit()
    finally:
        db.close()

    result = asyncio.run(
        materialize_news_trade_correlations_async(
            session_factory=Session,
            max_events=2,
            batch_size=1,
        )
    )

    assert result["processed"] == 2
    assert result["updated"] == 2
    assert result["batches"] == 2

    db = Session()
    try:
        events = db.query(NewsEvent).order_by(NewsEvent.id.asc()).all()
        assert [event.status for event in events] == [
            "pre_news_signal",
            "pre_news_signal",
        ]
        assert all(event.pre_news_trade_score >= 4.0 for event in events)
        assert all(
            "news_trade_correlation" in event.score_components for event in events
        )
    finally:
        db.close()
