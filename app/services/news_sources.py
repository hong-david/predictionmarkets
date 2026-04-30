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

from app.core.config import settings
from app.services.news_correlation import NormalizedArticle
from app.services.news_gdelt import tokenize_for_gdelt
from app.services.news_source_registry import (
    default_rss_feed_urls,
    source_for_url,
)


DEFAULT_RSS_FEEDS: tuple[str, ...] = default_rss_feed_urls()
NEWS_HEADERS = {
    "User-Agent": settings.news_user_agent,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}

_RSS_NAMESPACES = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_TAG_RE = re.compile(r"<[^>]+>")
_BARE_AMPERSAND_RE = re.compile(r"&(?!#?[a-zA-Z0-9]+;)")


@dataclass(frozen=True)
class SourceFetchResult:
    """Normalized output and diagnostics for one source fetch."""

    source_name: str
    articles: tuple[NormalizedArticle, ...]
    error: str | None = None
    source_key: str | None = None
    source_label: str | None = None
    source_tier: str | None = None
    authority_tier: str | None = None
    topic_tags: tuple[str, ...] = ()


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

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        root = ET.fromstring(_BARE_AMPERSAND_RE.sub("&amp;", xml_text))
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
        response = client.get(feed_url, headers=NEWS_HEADERS)
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
    source_tier: str | None = None,
) -> list[SourceFetchResult]:
    """Fetch multiple RSS/Atom feeds while isolating per-feed failures."""

    results: list[SourceFetchResult] = []
    for feed_url in feed_urls:
        source = source_for_url(feed_url)
        tier = source_tier or (source.source_tier if source else "rss")
        try:
            articles = fetch_rss_feed_articles(
                feed_url,
                since=since,
                until=until,
                limit=limit_per_feed,
                timeout=timeout,
                source_tier=tier,
            )
            results.append(
                SourceFetchResult(
                    feed_url,
                    tuple(articles),
                    source_key=source.key if source else None,
                    source_label=source.label if source else None,
                    source_tier=tier,
                    authority_tier=source.authority_tier if source else tier,
                    topic_tags=source.topic_tags if source else (),
                )
            )
        except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
            results.append(
                SourceFetchResult(
                    feed_url,
                    (),
                    error=f"{type(exc).__name__}: {exc}",
                    source_key=source.key if source else None,
                    source_label=source.label if source else None,
                    source_tier=tier,
                    authority_tier=source.authority_tier if source else tier,
                    topic_tags=source.topic_tags if source else (),
                )
            )
    return results


def fetch_federal_register_articles(
    *,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    timeout: float = 10.0,
) -> list[NormalizedArticle]:
    """Fetch recent Federal Register documents through the official JSON API."""

    params: dict[str, object] = {
        "per_page": max(1, min(int(limit), 1000)),
        "order": "newest",
    }
    if since is not None:
        params["conditions[publication_date][gte]"] = since.date().isoformat()
    if until is not None:
        params["conditions[publication_date][lte]"] = until.date().isoformat()

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(
            "https://www.federalregister.gov/api/v1/documents.json",
            params=params,
            headers=NEWS_HEADERS,
        )
        response.raise_for_status()
        data = response.json()

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []

    articles: list[NormalizedArticle] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = _clean_text(str(item.get("title") or ""))
        url = str(item.get("html_url") or item.get("pdf_url") or "").strip()
        if not title or not url:
            continue
        summary = _clean_text(
            str(item.get("abstract") or item.get("action") or "")
        )
        published = _parse_datetime(str(item.get("publication_date") or ""))
        agency_terms: list[str] = []
        agencies = item.get("agencies")
        if isinstance(agencies, list):
            for agency in agencies[:5]:
                if isinstance(agency, dict) and agency.get("name"):
                    agency_terms.extend(str(agency["name"]).split())
        keywords = tuple(
            dict.fromkeys(
                list(_article_keywords(title, summary))
                + agency_terms
                + [str(item.get("type") or "")]
            )
        )
        article = NormalizedArticle(
            canonical_url=url,
            title=title,
            published_at=published,
            first_seen_at=published,
            summary=summary,
            keywords=keywords,
            source_tier="official",
        )
        if _within_window(article, since=since, until=until):
            articles.append(article)
    return articles


def fetch_congress_articles(
    *,
    api_key: str,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    timeout: float = 10.0,
) -> list[NormalizedArticle]:
    """Fetch recent Congress.gov bill updates when an API key is configured."""

    params = {
        "api_key": api_key,
        "format": "json",
        "limit": max(1, min(int(limit), 250)),
        "sort": "updateDate+desc",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(
            "https://api.congress.gov/v3/bill",
            params=params,
            headers=NEWS_HEADERS,
        )
        response.raise_for_status()
        data = response.json()
    bills = data.get("bills") if isinstance(data, dict) else None
    if not isinstance(bills, list):
        return []

    articles: list[NormalizedArticle] = []
    for bill in bills:
        if not isinstance(bill, dict):
            continue
        title = _clean_text(str(bill.get("title") or "").strip())
        if not title:
            bill_type = str(bill.get("type") or "bill").strip()
            bill_number = str(bill.get("number") or "").strip()
            title = f"{bill_type} {bill_number}".strip()
        url = str(bill.get("url") or "").strip()
        if not title or not url:
            continue
        latest_action = bill.get("latestAction")
        summary = None
        action_date = None
        if isinstance(latest_action, dict):
            summary = _clean_text(str(latest_action.get("text") or ""))
            action_date = _parse_datetime(str(latest_action.get("actionDate") or ""))
        published = (
            _parse_datetime(str(bill.get("updateDateIncludingText") or ""))
            or _parse_datetime(str(bill.get("updateDate") or ""))
            or action_date
            or _parse_datetime(str(bill.get("introducedDate") or ""))
        )
        article = NormalizedArticle(
            canonical_url=url,
            title=title,
            published_at=published,
            first_seen_at=published,
            summary=summary,
            keywords=_article_keywords(title, summary),
            source_tier="official",
        )
        if _within_window(article, since=since, until=until):
            articles.append(article)
    return articles


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
