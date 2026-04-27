from datetime import datetime, timezone

from app.services.news_sources import dedupe_articles_by_url, parse_feed_articles
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
