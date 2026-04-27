"""Global news ingest and market-linking helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.db.models import Market, MarketNewsProfile, NewsArticle
from app.services.news_correlation import (
    NormalizedArticle,
    record_news_market_candidate,
    upsert_article,
    upsert_market_news_profile,
)
from app.services.news_clustering import (
    cluster_normalized_articles,
    score_components_for_cluster,
)
from app.services.news_candidates import news_market_candidates
from app.services.news_gdelt import tokenize_for_gdelt
from app.services.news_direction import components_with_market_direction
from app.services.news_relevance import hybrid_news_relevance
from app.services.news_sources import (
    DEFAULT_RSS_FEEDS,
    dedupe_articles_by_url,
    fetch_rss_articles,
)

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
    max_candidates: int | None = None,
    score_context: dict | None = None,
) -> int:
    article = db.query(NewsArticle).filter(NewsArticle.id == article_id).one()
    profiles = (
        db.query(MarketNewsProfile)
        .order_by(MarketNewsProfile.updated_at.desc())
        .limit(max_profiles)
        .all()
    )
    linked = 0
    candidates = news_market_candidates(
        article,
        profiles,
        max_candidates=max_candidates,
    )
    for candidate in candidates:
        profile = candidate.profile
        result = hybrid_news_relevance(article, profile)
        if result.score < min_relevance:
            continue
        components = components_with_market_direction(
            article,
            profile,
            result.components,
        )
        components["candidate_generation"] = candidate.as_score_component()
        if score_context:
            components.update(score_context)
        record_news_market_candidate(
            db,
            article_id=article_id,
            market_pk=profile.market_pk,
            relevance_score=result.score,
            score_components=components,
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
    include_gdelt: bool = True,
    rss_feeds: list[str] | tuple[str, ...] | None = None,
    limit_per_rss_feed: int = 50,
    max_candidates_per_article: int | None = None,
) -> dict:
    refreshed = refresh_market_news_profiles(db, max_markets=max_markets)
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=lookback_hours)
    fetched = articles
    provider_status = "injected" if articles is not None else ""
    fetch_errors: list[str] = []
    rss_feed_list = tuple(rss_feeds or DEFAULT_RSS_FEEDS)
    source_counts: dict[str, int] = {}

    if fetched is None:
        fetched = []
        if include_gdelt:
            try:
                gdelt_articles = fetch_gdelt_articles(
                    query=query,
                    since=since,
                    until=until,
                    limit=limit,
                )
                fetched.extend(gdelt_articles)
                source_counts["gdelt"] = len(gdelt_articles)
            except (httpx.HTTPError, ValueError) as exc:
                fetch_errors.append(f"gdelt: {type(exc).__name__}: {exc}")

        if rss_feed_list:
            rss_articles_seen = 0
            rss_errors = 0
            for result in fetch_rss_articles(
                rss_feed_list,
                since=since,
                until=until,
                limit_per_feed=limit_per_rss_feed,
            ):
                fetched.extend(result.articles)
                rss_articles_seen += len(result.articles)
                if result.error:
                    rss_errors += 1
                    fetch_errors.append(f"rss {result.source_name}: {result.error}")
            source_counts["rss"] = rss_articles_seen
            if rss_errors:
                source_counts["rss_errors"] = rss_errors

        fetched = dedupe_articles_by_url(fetched)
        active_sources = []
        if include_gdelt:
            active_sources.append("gdelt")
        if rss_feed_list:
            active_sources.append("rss")
        if active_sources:
            provider_status = "+".join(active_sources)
        if not fetched and fetch_errors:
            provider_status = "unavailable"

    clusters = cluster_normalized_articles(fetched)

    upserted = 0
    linked = 0
    for cluster in clusters:
        representative_id: int | None = None
        for article in cluster.articles:
            article_id = upsert_article(db, article)
            upserted += 1
            if article is cluster.representative:
                representative_id = article_id

        if representative_id is None and cluster.articles:
            # Defensive fallback. Representative should always come from articles.
            representative_id = upsert_article(db, cluster.representative)

        if representative_id is None:
            continue

        linked += link_article_to_markets(
            db,
            article_id=representative_id,
            min_relevance=min_relevance,
            max_profiles=max_profiles,
            max_candidates=max_candidates_per_article,
            score_context=score_components_for_cluster(cluster),
        )
    return {
        "profiles_refreshed": refreshed,
        "articles_seen": len(fetched),
        "article_clusters_seen": len(clusters),
        "articles_upserted": upserted,
        "news_events_linked": linked,
        "provider_status": provider_status or "none",
        "fetch_error": "; ".join(fetch_errors) if fetch_errors else None,
        "source_counts": source_counts,
        "query": query,
    }
