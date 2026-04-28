from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Market, MarketMetric, MarketNewsProfile, NewsArticle, NewsEvent
from app.services.search_index import (
    _news_result_matches_query,
    market_document,
    news_document,
    search_postgres,
)


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def test_market_document_includes_profile_terms() -> None:
    market = Market(
        id=1,
        platform="kalshi",
        market_id="KXBTC-TEST",
        event_id="KXBTC",
        title="Will Bitcoin trade above $100,000 by year end?",
        subtitle=None,
        status="active",
        category="crypto",
        manipulability_prior="high",
        classifier_tags=["bitcoin"],
        classifier_confidence="high",
    )
    metric = MarketMetric(market_pk=1, trade_count=12, anomaly_count=2)

    doc = market_document(market, metric)

    assert doc["market_id"] == "KXBTC-TEST"
    assert doc["trade_count"] == 12
    assert "bitcoin" in doc["keywords"]
    assert "above_threshold" in doc["keywords"]


def test_news_document_is_json_friendly() -> None:
    article = NewsArticle(
        id=7,
        canonical_url_hash="abc",
        canonical_url="https://example.com/story",
        domain="example.com",
        first_seen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        title="Bitcoin rallies after policy shift",
        keywords=["bitcoin", "policy"],
        entities=["Bitcoin"],
    )

    doc = news_document(article)

    assert doc["article_id"] == 7
    assert doc["canonical_url"] == "https://example.com/story"
    assert doc["first_seen_at"] == "2026-01-01T00:00:00+00:00"
    assert doc["keywords"] == ["bitcoin", "policy"]


def test_postgres_fallback_finds_related_markets_and_linked_news() -> None:
    db = _db()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    btc = Market(
        id=1,
        platform="kalshi",
        market_id="KXBTC-ABOVE",
        event_id="KXBTC",
        title="Will Bitcoin trade above $100,000 by year end?",
        subtitle=None,
        status="active",
        category="crypto",
        manipulability_prior="high",
        created_at=now,
        updated_at=now,
    )
    sports = Market(
        id=2,
        platform="kalshi",
        market_id="KXNFL-TEST",
        event_id="KXNFL",
        title="Will New York win the football game?",
        subtitle=None,
        status="active",
        category="sports_outcome",
        manipulability_prior="medium",
        created_at=now,
        updated_at=now,
    )
    article = NewsArticle(
        id=1,
        canonical_url_hash="hash",
        canonical_url="https://example.com/bitcoin-sec",
        domain="example.com",
        first_seen_at=now,
        title="SEC announces friendlier crypto stance as Bitcoin rises",
        summary="Digital asset markets rallied.",
        keywords=["sec", "crypto", "bitcoin"],
        entities=["Bitcoin", "SEC"],
    )
    db.add_all(
        [
            btc,
            sports,
            MarketMetric(market_pk=1, trade_count=5, anomaly_count=1),
            MarketMetric(market_pk=2, trade_count=5, anomaly_count=0),
            MarketNewsProfile(
                market_pk=1,
                normalized_keywords=[
                    "bitcoin",
                    "btc",
                    "crypto",
                    "above_threshold",
                ],
                entities=[],
                aliases=["KXBTC-ABOVE"],
                category="crypto",
                updated_at=now,
            ),
            MarketNewsProfile(
                market_pk=2,
                normalized_keywords=["football", "game"],
                entities=[],
                aliases=["KXNFL-TEST"],
                category="sports_outcome",
                updated_at=now,
            ),
            article,
            NewsEvent(
                id=1,
                article_id=1,
                market_pk=1,
                relevance_score=0.8,
                pre_news_trade_score=2.0,
                score_components={},
                created_at=now,
            ),
        ]
    )
    db.commit()

    markets, news = search_postgres(db, "SEC crypto Bitcoin", scope="all", limit=5)

    assert [m["market_id"] for m in markets][:1] == ["KXBTC-ABOVE"]
    assert any(n["article_id"] == 1 for n in news)
    linked = next(n for n in news if n["article_id"] == 1)["linked_markets"]
    assert linked[0]["market_id"] == "KXBTC-ABOVE"


def test_news_search_filter_rejects_fuzzy_unrelated_news() -> None:
    assert not _news_result_matches_query(
        {
            "title": "Venice opera house appoints incoming music director",
            "summary": "",
            "source": "example.com",
            "url": "https://example.com/opera",
            "linked_markets": [],
        },
        "kash patel fbi",
    )
    assert _news_result_matches_query(
        {
            "title": "Kash Patel faces new FBI oversight questions",
            "summary": "",
            "source": "example.com",
            "url": "https://example.com/fbi",
            "linked_markets": [],
        },
        "kash patel fbi",
    )
