from datetime import datetime, timezone

from app.services.news_source_registry import (
    DEFAULT_NEWS_SOURCES,
    apply_source_quality,
    default_rss_feed_urls,
)
from app.services.news_sources import (
    dedupe_articles_by_url,
    fetch_rss_articles,
    parse_feed_articles,
)
from app.services.news_correlation import NormalizedArticle


def test_parse_rss_feed_articles_extracts_headline_summary_and_time():
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>Trump appoints pro crypto regulator to lead SEC</title>
          <link>https://example.com/sec-chair</link>
          <description>The chair is viewed as friendly to digital assets.</description>
          <pubDate>Sun, 26 Apr 2026 12:30:00 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """

    articles = parse_feed_articles(xml, feed_url="https://example.com/rss", source_tier="rss_test")

    assert len(articles) == 1
    article = articles[0]
    assert article.canonical_url == "https://example.com/sec-chair"
    assert article.title == "Trump appoints pro crypto regulator to lead SEC"
    assert article.summary == "The chair is viewed as friendly to digital assets."
    assert article.published_at == datetime(2026, 4, 26, 12, 30, tzinfo=timezone.utc)
    assert article.source_tier == "rss_test"
    assert "crypto" in article.keywords


def test_parse_atom_feed_articles_extracts_href_link():
    xml = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Fed signals faster rate cuts</title>
        <link href="https://example.com/fed-cuts" />
        <summary>Risk assets rallied after the announcement.</summary>
        <updated>2026-04-26T13:00:00Z</updated>
      </entry>
    </feed>
    """

    articles = parse_feed_articles(xml, feed_url="https://example.com/atom")

    assert len(articles) == 1
    assert articles[0].canonical_url == "https://example.com/fed-cuts"
    assert articles[0].summary == "Risk assets rallied after the announcement."
    assert articles[0].published_at == datetime(2026, 4, 26, 13, tzinfo=timezone.utc)


def test_parse_feed_articles_recovers_from_bare_ampersand_in_link():
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>Agency publishes merger notice</title>
          <link>https://example.gov/document?agency=ftc&type=notice</link>
          <pubDate>Sun, 26 Apr 2026 12:30:00 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """

    articles = parse_feed_articles(xml, feed_url="https://example.gov/rss")

    assert len(articles) == 1
    assert articles[0].canonical_url == "https://example.gov/document?agency=ftc&type=notice"


def test_feed_window_filter_skips_old_items():
    xml = """
    <rss version="2.0"><channel>
      <item>
        <title>Old headline</title>
        <link>https://example.com/old</link>
        <pubDate>Sun, 26 Apr 2026 09:00:00 GMT</pubDate>
      </item>
      <item>
        <title>Current headline</title>
        <link>https://example.com/current</link>
        <pubDate>Sun, 26 Apr 2026 12:00:00 GMT</pubDate>
      </item>
    </channel></rss>
    """

    articles = parse_feed_articles(
        xml,
        feed_url="https://example.com/rss",
        since=datetime(2026, 4, 26, 11, tzinfo=timezone.utc),
        until=datetime(2026, 4, 26, 13, tzinfo=timezone.utc),
    )

    assert [article.title for article in articles] == ["Current headline"]


def test_dedupe_articles_by_url_keeps_first_copy():
    first = NormalizedArticle(canonical_url="https://example.com/a", title="First")
    duplicate = NormalizedArticle(canonical_url="https://example.com/a", title="Duplicate")
    second = NormalizedArticle(canonical_url="https://example.com/b", title="Second")

    assert dedupe_articles_by_url([first, duplicate, second]) == [first, second]


def test_default_source_registry_includes_official_feeds():
    feeds = default_rss_feed_urls()

    assert any(
        source.key == "federal_register_recent"
        and source.adapter == "federal_register_api"
        for source in DEFAULT_NEWS_SOURCES
    )
    assert any("browse-edgar" in feed and "output=atom" in feed for feed in feeds)
    assert any("medwatch/rss.xml" in feed.lower() for feed in feeds)


def test_fetch_rss_articles_uses_registry_source_tier(monkeypatch):
    captured: list[str | None] = []

    def fake_fetch(_feed_url, **kwargs):
        captured.append(kwargs.get("source_tier"))
        return []

    monkeypatch.setattr("app.services.news_sources.fetch_rss_feed_articles", fake_fetch)

    result = fetch_rss_articles(
        ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom",),
        since=None,
        until=None,
        limit_per_feed=10,
    )

    assert captured == ["official"]
    assert result[0].source_key == "sec_edgar_current_8k"
    assert result[0].authority_tier == "official"


def test_source_quality_boosts_only_plausibly_relevant_official_articles():
    official = NormalizedArticle(
        canonical_url="https://www.sec.gov/news/press-release/example",
        title="SEC announces crypto task force",
        source_tier="official",
    )
    broad = NormalizedArticle(
        canonical_url="https://rss.nytimes.com/example",
        title="Broad headline",
        source_tier="broad",
    )

    official_score, official_component = apply_source_quality(0.32, official)
    broad_score, broad_component = apply_source_quality(0.32, broad)

    assert official_score > 0.35
    assert official_component["applied"] is True
    assert broad_score == 0.32
    assert broad_component["applied"] is False
