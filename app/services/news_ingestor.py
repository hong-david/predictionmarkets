"""Global news ingest and market-linking helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import time
from typing import Any

import httpx
from sqlalchemy import case, or_
from sqlalchemy.orm import Session

from app.db.models import Market, MarketNewsProfile, NewsArticle
from app.services.news_correlation import (
    NEWS_PROFILE_EXCLUDED_CATEGORIES,
    NormalizedArticle,
    is_news_profile_candidate,
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
from app.services.news_link_scoring import score_article_market_link
from app.services.news_profile_index import (
    build_news_profile_index,
    candidate_pool_for_article,
)
from app.services.news_sources import (
    DEFAULT_RSS_FEEDS,
    dedupe_articles_by_url,
    fetch_congress_articles,
    fetch_federal_register_articles,
    fetch_rss_articles,
)
from app.services.news_source_registry import (
    DEFAULT_NEWS_SOURCES,
    source_registry_diagnostics,
)

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_QUERY_PAUSE_SECONDS = 1.0
DEFAULT_GLOBAL_NEWS_QUERY = (
    "breaking OR announced OR reports OR decision OR injury OR earnings "
    "OR merger OR court OR Fed OR CPI"
)
DEFAULT_GLOBAL_NEWS_QUERIES: tuple[str, ...] = (
    DEFAULT_GLOBAL_NEWS_QUERY,
    "Federal Reserve OR FOMC OR inflation OR CPI OR PCE OR jobs report OR unemployment OR GDP",
    "SEC OR CFTC OR Treasury OR DOJ OR lawsuit OR court ruling OR antitrust OR merger",
    "Bitcoin OR Ethereum OR crypto OR ETF OR stablecoin OR blockchain OR digital asset",
    "election OR primary OR polling OR candidate OR governor OR senate OR supreme court",
    "injury OR lineup OR roster OR suspension OR transfer OR playoff OR championship",
    "oil OR natural gas OR OPEC OR EIA OR hurricane OR wildfire OR weather warning",
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
    timeout: float = 20.0,
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
        response = client.get(
            GDELT_DOC_URL,
            params=params,
            headers={"User-Agent": "predictionmarkets-news-ingestor/1.0"},
        )
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


def _gdelt_queries_for_sweep(query: str) -> tuple[str, ...]:
    """Use several broad-but-themed queries for the default global sweep.

    A single GDELT query is capped and tends to overfit the most generic
    headlines. Multiple themed queries widen collection while downstream
    URL dedupe and market-link scoring still keep unrelated news out of the UI.
    """

    cleaned = (query or "").strip()
    if not cleaned or cleaned == DEFAULT_GLOBAL_NEWS_QUERY:
        return DEFAULT_GLOBAL_NEWS_QUERIES
    return (cleaned,)


def refresh_market_news_profiles(
    db: Session,
    *,
    max_markets: int,
) -> int:
    missing_profile_first = case(
        (MarketNewsProfile.market_pk.is_(None), 0),
        else_=1,
    )
    rows = (
        db.query(Market)
        .outerjoin(MarketNewsProfile, MarketNewsProfile.market_pk == Market.id)
        .filter(or_(Market.status.is_(None), Market.status != "unknown"))
        .filter(
            or_(
                Market.category.is_(None),
                Market.category.notin_(tuple(NEWS_PROFILE_EXCLUDED_CATEGORIES)),
            )
        )
        .filter(Market.title != Market.market_id)
        .order_by(
            missing_profile_first.asc(),
            MarketNewsProfile.updated_at.asc().nullsfirst(),
            Market.updated_at.desc(),
        )
        .limit(max_markets)
        .all()
    )
    rows = [market for market in rows if is_news_profile_candidate(market)]
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
    profiles: list[MarketNewsProfile] | None = None,
) -> int:
    article = db.query(NewsArticle).filter(NewsArticle.id == article_id).one()
    if profiles is None:
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
        link_score = score_article_market_link(
            article,
            profile,
            min_relevance=min_relevance,
            candidate_component=candidate.as_score_component(),
            score_context=score_context,
        )
        if link_score is None:
            continue
        record_news_market_candidate(
            db,
            article_id=article_id,
            market_pk=profile.market_pk,
            relevance_score=link_score.relevance_score,
            score_components=link_score.components,
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
    source_counts: dict[str, Any] = {}

    if fetched is None:
        fetched = []
        if include_gdelt:
            gdelt_articles_seen = 0
            gdelt_errors = 0
            for gdelt_query in _gdelt_queries_for_sweep(query):
                try:
                    gdelt_articles = fetch_gdelt_articles(
                        query=gdelt_query,
                        since=since,
                        until=until,
                        limit=limit,
                    )
                    fetched.extend(gdelt_articles)
                    gdelt_articles_seen += len(gdelt_articles)
                except (httpx.HTTPError, ValueError) as exc:
                    gdelt_errors += 1
                    fetch_errors.append(
                        f"gdelt {gdelt_query[:80]}: {type(exc).__name__}: {exc}"
                    )
                time.sleep(GDELT_QUERY_PAUSE_SECONDS)
            source_counts["gdelt"] = gdelt_articles_seen
            source_counts["gdelt_queries"] = len(_gdelt_queries_for_sweep(query))
            if gdelt_errors:
                source_counts["gdelt_errors"] = gdelt_errors

        if rss_feed_list:
            rss_articles_seen = 0
            rss_errors = 0
            rss_feed_counts: dict[str, int] = {}
            rss_results = fetch_rss_articles(
                rss_feed_list,
                since=since,
                until=until,
                limit_per_feed=limit_per_rss_feed,
            )
            for result in rss_results:
                fetched.extend(result.articles)
                rss_articles_seen += len(result.articles)
                rss_feed_counts[result.source_name] = len(result.articles)
                if result.error:
                    rss_errors += 1
                    fetch_errors.append(f"rss {result.source_name}: {result.error}")
            source_counts["rss"] = rss_articles_seen
            source_counts["rss_feeds"] = len(rss_feed_list)
            source_counts["rss_feed_counts"] = rss_feed_counts
            source_counts["rss_feed_details"] = [
                {
                    "source": result.source_name,
                    "key": result.source_key,
                    "label": result.source_label,
                    "count": len(result.articles),
                    "error": result.error,
                    "source_tier": result.source_tier,
                    "authority_tier": result.authority_tier,
                    "topic_tags": list(result.topic_tags),
                }
                for result in rss_results
            ]
            if rss_errors:
                source_counts["rss_errors"] = rss_errors

        federal_register_sources = [
            source
            for source in DEFAULT_NEWS_SOURCES
            if source.adapter == "federal_register_api" and source.available
        ]
        if federal_register_sources:
            federal_register_seen = 0
            federal_register_errors = 0
            federal_register_details: list[dict[str, object]] = []
            for source in federal_register_sources:
                try:
                    federal_register_articles = fetch_federal_register_articles(
                        since=since,
                        until=until,
                        limit=limit_per_rss_feed,
                    )
                    fetched.extend(federal_register_articles)
                    federal_register_seen += len(federal_register_articles)
                    federal_register_details.append(
                        {
                            "source": source.url,
                            "key": source.key,
                            "label": source.label,
                            "count": len(federal_register_articles),
                            "error": None,
                            "source_tier": source.source_tier,
                            "authority_tier": source.authority_tier,
                            "topic_tags": list(source.topic_tags),
                        }
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    federal_register_errors += 1
                    error = f"{type(exc).__name__}: {exc}"
                    fetch_errors.append(f"federal_register_api {source.url}: {error}")
                    federal_register_details.append(
                        {
                            "source": source.url,
                            "key": source.key,
                            "label": source.label,
                            "count": 0,
                            "error": error,
                            "source_tier": source.source_tier,
                            "authority_tier": source.authority_tier,
                            "topic_tags": list(source.topic_tags),
                        }
                    )
            source_counts["federal_register_api"] = federal_register_seen
            source_counts["federal_register_api_sources"] = len(
                federal_register_sources
            )
            source_counts["federal_register_api_details"] = federal_register_details
            if federal_register_errors:
                source_counts["federal_register_api_errors"] = (
                    federal_register_errors
                )

        congress_key = os.getenv("CONGRESS_API_KEY")
        if congress_key:
            try:
                congress_articles = fetch_congress_articles(
                    api_key=congress_key,
                    since=since,
                    until=until,
                    limit=limit,
                )
                fetched.extend(congress_articles)
                source_counts["congress_api"] = len(congress_articles)
            except (httpx.HTTPError, ValueError) as exc:
                source_counts["congress_api_errors"] = 1
                fetch_errors.append(
                    f"congress_api: {type(exc).__name__}: {exc}"
                )
        else:
            source_counts["congress_api_disabled"] = 1

        fetched = dedupe_articles_by_url(fetched)
        active_sources = []
        if include_gdelt:
            active_sources.append("gdelt")
        if rss_feed_list:
            active_sources.append("rss")
        if "federal_register_api" in source_counts:
            active_sources.append("federal_register")
        if "congress_api" in source_counts:
            active_sources.append("congress")
        if active_sources:
            provider_status = "+".join(active_sources)
        if not fetched and fetch_errors:
            provider_status = "unavailable"

    clusters = cluster_normalized_articles(fetched)
    profile_rows = (
        db.query(MarketNewsProfile)
        .order_by(MarketNewsProfile.updated_at.desc())
        .limit(max_profiles)
        .all()
        if clusters
        else []
    )
    profile_index = build_news_profile_index(profile_rows)

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

        representative = (
            db.query(NewsArticle).filter(NewsArticle.id == representative_id).one()
        )
        profile_pool = candidate_pool_for_article(representative, profile_index)
        linked += link_article_to_markets(
            db,
            article_id=representative_id,
            min_relevance=min_relevance,
            max_profiles=max_profiles,
            max_candidates=max_candidates_per_article,
            score_context=score_components_for_cluster(cluster),
            profiles=profile_pool,
        )
    return {
        "profiles_refreshed": refreshed,
        "profiles_loaded": len(profile_rows),
        "articles_seen": len(fetched),
        "article_clusters_seen": len(clusters),
        "articles_upserted": upserted,
        "news_events_linked": linked,
        "provider_status": provider_status or "none",
        "fetch_error": "; ".join(fetch_errors) if fetch_errors else None,
        "source_counts": source_counts,
        "source_registry": source_registry_diagnostics(),
        "query": query,
    }
