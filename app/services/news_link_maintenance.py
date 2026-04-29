"""Maintenance helpers for re-scoring stored news/market links."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Market, MarketNewsProfile, NewsArticle, NewsEvent
from app.services.news_candidates import news_market_candidates
from app.services.news_correlation import profile_for_market, record_news_market_candidate
from app.services.news_ingestor import refresh_market_news_profiles
from app.services.news_link_scoring import score_article_market_link
from app.services.news_profile_index import (
    build_news_profile_index,
    candidate_pool_for_article,
)


def revalidate_stored_news_links(
    db: Session,
    *,
    limit: int = 5000,
    min_relevance: float = 0.35,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-score existing links and remove rows that fail current guardrails."""

    rows = (
        db.query(NewsEvent, NewsArticle, Market)
        .join(NewsArticle, NewsArticle.id == NewsEvent.article_id)
        .join(Market, Market.id == NewsEvent.market_pk)
        .order_by(NewsEvent.id.asc())
        .limit(limit)
        .all()
    )
    kept = 0
    removed = 0
    updated = 0
    removed_samples: list[dict[str, Any]] = []
    for event, article, market in rows:
        profile = MarketNewsProfile(**profile_for_market(market))
        link_score = score_article_market_link(
            article,
            profile,
            min_relevance=min_relevance,
        )
        if link_score is None:
            removed += 1
            if len(removed_samples) < 10:
                removed_samples.append(
                    {
                        "event_id": event.id,
                        "market_id": market.market_id,
                        "market_title": market.title,
                        "article_title": article.title,
                        "previous_score": float(event.relevance_score or 0.0),
                    }
                )
            if not dry_run:
                db.delete(event)
            continue
        kept += 1
        existing_components = event.score_components if isinstance(event.score_components, dict) else {}
        merged_components = {
            **existing_components,
            **link_score.components,
        }
        score_changed = abs(float(event.relevance_score or 0.0) - link_score.relevance_score) > 0.0001
        components_changed = merged_components != existing_components
        if score_changed or components_changed:
            updated += 1
            if not dry_run:
                event.relevance_score = link_score.relevance_score
                event.score_components = merged_components
    return {
        "processed": len(rows),
        "kept": kept,
        "removed": removed,
        "updated": updated,
        "dry_run": dry_run,
        "removed_samples": removed_samples,
    }


def relink_stored_articles(
    db: Session,
    *,
    article_limit: int = 5000,
    max_markets: int = 12000,
    max_profiles: int = 12000,
    max_candidates_per_article: int | None = 150,
    max_factor_profiles_per_category: int = 5000,
    min_relevance: float = 0.35,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run current market-link scoring across stored articles."""

    profiles_refreshed = refresh_market_news_profiles(db, max_markets=max_markets)
    profiles = (
        db.query(MarketNewsProfile)
        .order_by(MarketNewsProfile.updated_at.desc())
        .limit(max_profiles)
        .all()
    )
    profile_index = build_news_profile_index(profiles)
    articles = (
        db.query(NewsArticle)
        .order_by(NewsArticle.first_seen_at.desc(), NewsArticle.id.desc())
        .limit(article_limit)
        .all()
    )
    linked = 0
    for article in articles:
        candidate_pool = candidate_pool_for_article(
            article,
            profile_index,
            max_factor_profiles_per_category=max_factor_profiles_per_category,
        )
        if not candidate_pool:
            continue
        candidates = news_market_candidates(
            article,
            candidate_pool,
            max_candidates=max_candidates_per_article,
        )
        for candidate in candidates:
            link_score = score_article_market_link(
                article,
                candidate.profile,
                min_relevance=min_relevance,
                candidate_component=candidate.as_score_component(),
            )
            if link_score is None:
                continue
            record_news_market_candidate(
                db,
                article_id=article.id,
                market_pk=candidate.profile.market_pk,
                relevance_score=link_score.relevance_score,
                score_components=link_score.components,
            )
            linked += 1
    if dry_run:
        db.rollback()
    return {
        "profiles_refreshed": profiles_refreshed,
        "profiles_loaded": len(profiles),
        "articles_processed": len(articles),
        "links_created_or_updated": linked,
        "min_relevance": min_relevance,
        "dry_run": dry_run,
    }
