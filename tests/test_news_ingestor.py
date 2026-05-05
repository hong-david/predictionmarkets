from datetime import datetime, timezone

import httpx

from app.db.models import Market
from app.services.news_correlation import (
    NormalizedArticle,
    is_news_profile_candidate,
    market_news_search_query,
    profile_for_market,
)
from app.services.news_ingestor import ingest_global_news, normalize_gdelt_article
from app.services.news_sources import SourceFetchResult


def test_normalize_gdelt_article_extracts_core_fields() -> None:
    article = normalize_gdelt_article(
        {
            "url": "https://example.com/story",
            "title": "Fed announces rate decision",
            "seendate": "20260427123000",
            "language": "English",
        }
    )

    assert article is not None
    assert article.canonical_url == "https://example.com/story"
    assert article.title == "Fed announces rate decision"
    assert article.published_at == datetime(2026, 4, 27, 12, 30, tzinfo=timezone.utc)
    assert "Fed" in article.keywords or "fed" in article.keywords


def test_normalize_gdelt_article_skips_missing_url_or_title() -> None:
    assert normalize_gdelt_article({"title": "No URL"}) is None
    assert normalize_gdelt_article({"url": "https://example.com"}) is None


def test_ingest_global_news_fails_open_when_gdelt_unavailable(monkeypatch) -> None:
    def fake_refresh(_db, *, max_markets: int) -> int:
        assert max_markets == 10
        return 7

    def fake_fetch(**_kwargs):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(
        "app.services.news_ingestor.refresh_market_news_profiles", fake_refresh
    )
    monkeypatch.setattr("app.services.news_ingestor.fetch_gdelt_articles", fake_fetch)
    monkeypatch.setattr("app.services.news_ingestor.fetch_rss_articles", lambda *a, **k: [])

    # Keep this test focused on the GDELT fail-open path. Extra providers can
    # produce articles and then require a real db.query(...) later in the flow.
    monkeypatch.setattr("app.services.news_ingestor.DEFAULT_NEWS_SOURCES", ())
    monkeypatch.delenv("CONGRESS_API_KEY", raising=False)

    result = ingest_global_news(
        object(),
        max_markets=10,
        max_profiles=10,
        limit=5,
    )

    assert result["profiles_refreshed"] == 7
    assert result["articles_seen"] == 0
    assert result["articles_upserted"] == 0
    assert result["news_events_linked"] == 0
    assert result["provider_status"] == "unavailable"
    assert "ConnectTimeout" in result["fetch_error"]


def test_ingest_global_news_keeps_partial_rss_errors_in_source_counts(
    monkeypatch,
) -> None:
    article = NormalizedArticle(
        canonical_url="https://example.com/story",
        title="Fed announces a rate decision",
        published_at=datetime(2026, 4, 27, 12, 30, tzinfo=timezone.utc),
    )

    monkeypatch.setattr(
        "app.services.news_ingestor.refresh_market_news_profiles",
        lambda _db, *, max_markets: 0,
    )
    monkeypatch.setattr(
        "app.services.news_ingestor.fetch_rss_articles",
        lambda *a, **k: [
            SourceFetchResult(source_name="good", articles=(article,)),
            SourceFetchResult(source_name="npr", articles=(), error="ReadTimeout"),
        ],
    )
    monkeypatch.setattr(
        "app.services.news_ingestor.cluster_normalized_articles",
        lambda _articles: [],
    )
    monkeypatch.setattr("app.services.news_ingestor.DEFAULT_NEWS_SOURCES", ())

    result = ingest_global_news(
        object(),
        include_gdelt=False,
        rss_feeds=("good", "npr"),
        max_markets=10,
        max_profiles=10,
        limit=5,
    )

    assert result["articles_seen"] == 1
    assert result["provider_status"] == "rss"
    assert result["fetch_error"] is None
    assert result["source_counts"]["rss_errors"] == 1


def test_news_profile_scope_includes_retention_excluded_news_markets() -> None:
    market = Market(
        id=1,
        platform="kalshi",
        market_id="KXBTC-TEST",
        title="Will Bitcoin trade above $100,000 by year end?",
        status="out_of_scope",
        category="crypto_strike",
        classifier_layer="prefix_rule",
        classifier_confidence="high",
        classifier_tags=["crypto", "btc", "public_underlying"],
    )

    assert is_news_profile_candidate(market) is True

    profile = profile_for_market(market)
    assert "bitcoin" in profile["normalized_keywords"]
    assert "btc" in profile["normalized_keywords"]
    assert "above_threshold" in profile["normalized_keywords"]


def test_news_profile_ignores_untrusted_classifier_tags_as_anchors() -> None:
    market = Market(
        id=1,
        platform="kalshi",
        market_id="KXNASDAQ100U-TEST",
        title="Will the Nasdaq-100 be above 28699.99 at 4pm EDT?",
        status="out_of_scope",
        category="crypto_strike",
        classifier_layer="knn_embedding",
        classifier_confidence="medium",
        classifier_tags=["crypto", "btc", "public_underlying"],
    )

    profile = profile_for_market(market)

    assert "nasdaq-100" in profile["normalized_keywords"]
    assert "above_threshold" in profile["normalized_keywords"]
    assert "btc" not in profile["normalized_keywords"]
    assert "bitcoin" not in profile["normalized_keywords"]
    assert "crypto" not in profile["normalized_keywords"]


def test_news_profile_drops_structural_classifier_tags_for_corporate_metrics() -> None:
    market = Market(
        id=4,
        platform="kalshi",
        market_id="KXMAR-26MAYROOMS-1760000",
        title="Will Marriott International report above 1.76 million total rooms in Q1 2026?",
        status="active",
        category="corporate",
        classifier_layer="prefix_rule",
        classifier_confidence="high",
        classifier_tags=["merger", "single_actor_leverage"],
    )

    profile = profile_for_market(market)

    assert "marriott" in profile["normalized_keywords"]
    assert "rooms" in profile["normalized_keywords"]
    assert "above_threshold" in profile["normalized_keywords"]
    assert "merger" not in profile["normalized_keywords"]
    assert "single_actor_leverage" not in profile["normalized_keywords"]
    assert "international" not in profile["normalized_keywords"]
    assert "report" not in profile["normalized_keywords"]
    assert "million" not in profile["normalized_keywords"]


def test_news_profile_scope_still_excludes_unhydrated_and_exotic_markets() -> None:
    unknown = Market(
        id=1,
        platform="kalshi",
        market_id="KXUNKNOWN",
        title="KXUNKNOWN",
        status="unknown",
        category="other",
    )
    exotic = Market(
        id=2,
        platform="kalshi",
        market_id="KXMVECROSSCATEGORY-TEST",
        title="Will this multi-leg combo resolve yes?",
        status="out_of_scope",
        category="exotic_combo",
    )

    assert is_news_profile_candidate(unknown) is False
    assert is_news_profile_candidate(exotic) is False


def test_market_news_search_query_drops_dates_ticker_and_generic_terms() -> None:
    market = Market(
        id=3,
        platform="kalshi",
        market_id="KXKASHOUT-26APR-MAY01",
        title="Will Kash Patel leaves as FBI Director before May 1, 2026?",
        status="active",
        category="politics",
    )

    query = market_news_search_query(market)

    assert query == "kash patel fbi"
