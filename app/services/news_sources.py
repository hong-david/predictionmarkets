"""News source adapters that normalize into ``NormalizedArticle``.

The pipeline should not treat one vendor/API as the whole news universe.  This
module adds a small adapter layer for additional headline/summary feeds while
keeping the storage model unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import html
import re
import xml.etree.ElementTree as ET

import httpx

from app.services.news_correlation import NormalizedArticle
from app.services.news_gdelt import tokenize_for_gdelt


DEFAULT_RSS_FEEDS: tuple[str, ...] = (
    # Public agencies / official releases.
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.sec.gov/news/pressreleases.rss",
    "https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
    "https://www.cftc.gov/RSS/RSSENF/rssenf.xml",
    "https://www.ftc.gov/feeds/press-release.xml",
    "https://www.ftc.gov/feeds/press-release-competition.xml",
    "https://www.bls.gov/feed/empsit.rss",
    "https://www.bls.gov/feed/cpi.rss",
    "https://www.bls.gov/feed/bls_latest.rss",
    "https://www.eia.gov/rss/todayinenergy.xml",
    # Crypto / markets.
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    # Broad news.
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "https://www.theguardian.com/world/rss",
    "https://www.theguardian.com/us-news/rss",
    "https://www.theguardian.com/business/rss",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "https://www.npr.org/rss/rss.php?id=1001",
    "https://www.npr.org/rss/rss.php?id=1006",
    "https://rss.politico.com/politics-news.xml",
    # Markets / business wires with useful headlines.
    "https://finance.yahoo.com/news/rssindex",
    "https://www.marketwatch.com/rss/topstories",
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.cnbc.com/id/10000664/device/rss/rss.html",
    "https://www.cnbc.com/id/10000113/device/rss/rss.html",
    # Weather / public agencies.
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
    "https://www.noaa.gov/rss.xml",
    "https://www.nhc.noaa.gov/index-at.xml",
    "https://www.nhc.noaa.gov/index-ep.xml",
    # Sports/injury/newswire style feeds. Individual feed failures are isolated.
    "https://www.espn.com/espn/rss/news",
    "https://www.espn.com/espn/rss/nfl/news",
    "https://www.espn.com/espn/rss/nba/news",
    "https://www.espn.com/espn/rss/mlb/news",
    "https://www.espn.com/espn/rss/nhl/news",
    "https://www.espn.com/espn/rss/soccer/news",
    "https://www.theguardian.com/sport/rss",
    "https://www.cbssports.com/rss/headlines/",
    "https://www.mlb.com/feeds/news/rss.xml",
)

_RSS_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class SourceFetchResult:
    """Normalized output and diagnostics for one source fetch."""

    source_name: str
    articles: tuple[NormalizedArticle, ...]
    error: str | None = None


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = _TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = " ".join(text.split())
    return text or None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _find_text(element: ET.Element, *paths: str) -> str | None:
    for path in paths:
        found = element.find(path, _RSS_NAMESPACES)
        if found is not None and found.text:
            return found.text
    return None


def _find_link(element: ET.Element, *paths: str) -> str | None:
    for path in paths:
        found = element.find(path, _RSS_NAMESPACES)
        if found is None:
            continue
        href = found.attrib.get("href")
        if href:
            return href
        if found.text:
            return found.text
    return None


def _article_keywords(title: str, summary: str | None) -> tuple[str, ...]:
    text = title if not summary else f"{title} {summary}"
    return tuple(tokenize_for_gdelt(text, max_tokens=16).split())


def _within_window(
    article: NormalizedArticle,
    *,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    ts = article.published_at or article.first_seen_at
    if ts is None:
        return True
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


def parse_feed_articles(
    xml_text: str,
    *,
    feed_url: str,
    source_tier: str = "rss",
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> list[NormalizedArticle]:
    """Parse RSS/Atom XML into normalized headline/summary records."""

    root = ET.fromstring(xml_text)
    if root.tag.endswith("feed"):
        entries = root.findall("atom:entry", _RSS_NAMESPACES) or root.findall("entry")
        parsed = [_normalize_atom_entry(e, feed_url=feed_url, source_tier=source_tier) for e in entries]
    else:
        items = root.findall("./channel/item") or root.findall(".//item")
        parsed = [_normalize_rss_item(i, feed_url=feed_url, source_tier=source_tier) for i in items]

    articles = [a for a in parsed if a is not None and _within_window(a, since=since, until=until)]
    if limit is not None:
        return articles[: max(0, limit)]
    return articles


def _normalize_rss_item(
    item: ET.Element,
    *,
    feed_url: str,
    source_tier: str,
) -> NormalizedArticle | None:
    title = _clean_text(_find_text(item, "title"))
    link = _clean_text(_find_link(item, "link", "guid"))
    if not title or not link:
        return None
    summary = _clean_text(
        _find_text(item, "description", "content:encoded", "summary")
    )
    published = _parse_datetime(_find_text(item, "pubDate", "dc:date", "published"))
    return NormalizedArticle(
        canonical_url=link,
        title=title,
        published_at=published,
        first_seen_at=published,
        summary=summary,
        keywords=_article_keywords(title, summary),
        source_tier=source_tier,
    )


def _normalize_atom_entry(
    entry: ET.Element,
    *,
    feed_url: str,
    source_tier: str,
) -> NormalizedArticle | None:
    title = _clean_text(_find_text(entry, "atom:title", "title"))
    link = _clean_text(_find_link(entry, "atom:link", "link", "atom:id", "id"))
    if not title or not link:
        return None
    summary = _clean_text(
        _find_text(entry, "atom:summary", "summary", "atom:content", "content")
    )
    published = _parse_datetime(
        _find_text(entry, "atom:published", "published", "atom:updated", "updated")
    )
    return NormalizedArticle(
        canonical_url=link,
        title=title,
        published_at=published,
        first_seen_at=published,
        summary=summary,
        keywords=_article_keywords(title, summary),
        source_tier=source_tier,
    )


def fetch_rss_feed_articles(
    feed_url: str,
    *,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    timeout: float = 10.0,
    source_tier: str = "rss",
) -> list[NormalizedArticle]:
    """Fetch one RSS/Atom feed and normalize headline/summary metadata."""

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(feed_url, headers={"User-Agent": "predictionmarkets-news-ingestor/1.0"})
        response.raise_for_status()
    return parse_feed_articles(
        response.text,
        feed_url=feed_url,
        source_tier=source_tier,
        since=since,
        until=until,
        limit=limit,
    )


def fetch_rss_articles(
    feed_urls: list[str] | tuple[str, ...],
    *,
    since: datetime | None,
    until: datetime | None,
    limit_per_feed: int,
    timeout: float = 10.0,
    source_tier: str = "rss",
) -> list[SourceFetchResult]:
    """Fetch multiple RSS/Atom feeds while isolating per-feed failures."""

    results: list[SourceFetchResult] = []
    for feed_url in feed_urls:
        try:
            articles = fetch_rss_feed_articles(
                feed_url,
                since=since,
                until=until,
                limit=limit_per_feed,
                timeout=timeout,
                source_tier=source_tier,
            )
            results.append(SourceFetchResult(feed_url, tuple(articles)))
        except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
            results.append(
                SourceFetchResult(
                    feed_url,
                    (),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return results


def dedupe_articles_by_url(articles: list[NormalizedArticle]) -> list[NormalizedArticle]:
    """Keep the first copy of each canonical URL before clustering."""

    seen: set[str] = set()
    out: list[NormalizedArticle] = []
    for article in articles:
        key = article.canonical_url.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(article)
    return out
