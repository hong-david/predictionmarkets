"""Global news ingest and market-linking helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.db.models import Market, MarketNewsProfile, NewsArticle
from app.services.news_correlation import (
    NormalizedArticle,
    lexical_relevance,
    record_news_market_candidate,
    upsert_article,
    upsert_market_news_profile,
)
from app.services.news_gdelt import tokenize_for_gdelt

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_GLOBAL_NEWS_QUERY = (
    "breaking OR announced OR reports OR decision OR injury OR earnings "
    "OR merger OR court OR Fed OR CPI"
)


def _parse_gdelt_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M%S%f"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_gdelt_article(item: dict[str, Any]) -> NormalizedArticle | None:
    url = item.get("url")
    title = item.get("title")
    if not url or not title:
        return None
    summary = item.get("summary") or item.get("snippet")
    seen = _parse_gdelt_ts(item.get("seendate"))
    keywords = tuple(tokenize_for_gdelt(str(title), max_tokens=12).split())
    return NormalizedArticle(
        canonical_url=str(url),
        title=str(title),
        published_at=seen,
        first_seen_at=seen,
        summary=str(summary) if summary else None,
        language=item.get("language"),
        keywords=keywords,
        source_tier="gdelt",
    )


def fetch_gdelt_articles(
    *,
    query: str,
    since: datetime,
    until: datetime,
    limit: int,
    timeout: float = 10.0,
) -> list[NormalizedArticle]:
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": max(1, min(limit, 250)),
        "sort": "hybridrel",
        "startdatetime": since.strftime("%Y%m%d%H%M%S"),
        "enddatetime": until.strftime("%Y%m%d%H%M%S"),
    }
    with httpx.Client(timeout=timeout) as client:
        response = client.get(GDELT_DOC_URL, params=params)
        response.raise_for_status()
        data = response.json()
    articles = data.get("articles") if isinstance(data, dict) else None
    if not isinstance(articles, list):
        return []
    out: list[NormalizedArticle] = []
    for item in articles:
        if isinstance(item, dict):
            article = normalize_gdelt_article(item)
            if article is not None:
                out.append(article)
    return out


def refresh_market_news_profiles(
    db: Session,
    *,
    max_markets: int,
) -> int:
    rows = (
        db.query(Market)
        .filter(Market.status.notin_(("unknown", "out_of_scope")))
        .filter(Market.title != Market.market_id)
        .order_by(Market.updated_at.desc())
        .limit(max_markets)
        .all()
    )
    for market in rows:
        upsert_market_news_profile(db, market)
    return len(rows)


def link_article_to_markets(
    db: Session,
    *,
    article_id: int,
    min_relevance: float,
    max_profiles: int,
) -> int:
    article = db.query(NewsArticle).filter(NewsArticle.id == article_id).one()
    profiles = (
        db.query(MarketNewsProfile)
        .order_by(MarketNewsProfile.updated_at.desc())
        .limit(max_profiles)
        .all()
    )
    linked = 0
    for profile in profiles:
        score = lexical_relevance(article, profile)
        if score < min_relevance:
            continue
        record_news_market_candidate(
            db,
            article_id=article_id,
            market_pk=profile.market_pk,
            relevance_score=score,
            score_components={"lexical_relevance": score},
        )
        linked += 1
    return linked


def ingest_global_news(
    db: Session,
    *,
    query: str = DEFAULT_GLOBAL_NEWS_QUERY,
    lookback_hours: int = 6,
    limit: int = 100,
    max_markets: int = 5000,
    max_profiles: int = 5000,
    min_relevance: float = 0.35,
    articles: list[NormalizedArticle] | None = None,
) -> dict:
    refreshed = refresh_market_news_profiles(db, max_markets=max_markets)
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=lookback_hours)
    fetched = articles
    provider_status = "injected" if articles is not None else "gdelt"
    fetch_error: str | None = None
    if fetched is None:
        try:
            fetched = fetch_gdelt_articles(
                query=query,
                since=since,
                until=until,
                limit=limit,
            )
        except (httpx.HTTPError, ValueError) as exc:
            fetched = []
            provider_status = "unavailable"
            fetch_error = f"{type(exc).__name__}: {exc}"

    upserted = 0
    linked = 0
    for article in fetched:
        article_id = upsert_article(db, article)
        upserted += 1
        linked += link_article_to_markets(
            db,
            article_id=article_id,
            min_relevance=min_relevance,
            max_profiles=max_profiles,
        )
    return {
        "profiles_refreshed": refreshed,
        "articles_seen": len(fetched),
        "articles_upserted": upserted,
        "news_events_linked": linked,
        "provider_status": provider_status,
        "fetch_error": fetch_error,
        "query": query,
    }
