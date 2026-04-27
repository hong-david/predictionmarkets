from datetime import datetime, timezone

import httpx

from app.services.news_ingestor import ingest_global_news, normalize_gdelt_article


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
