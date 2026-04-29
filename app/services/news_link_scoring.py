"""Shared article-to-market link scoring used by ingest and read paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.models import MarketNewsProfile, NewsArticle
from app.services.news_category_gate import category_news_gate
from app.services.news_direction import components_with_market_direction
from app.services.news_relevance import hybrid_news_relevance
from app.services.news_source_registry import apply_source_quality


@dataclass(frozen=True)
class NewsLinkScore:
    relevance_score: float
    components: dict[str, Any]


def score_article_market_link(
    article: NewsArticle,
    profile: MarketNewsProfile,
    *,
    min_relevance: float = 0.35,
    candidate_component: dict[str, Any] | None = None,
    score_context: dict[str, Any] | None = None,
) -> NewsLinkScore | None:
    """Score one article/profile pair and apply precision guardrails."""

    result = hybrid_news_relevance(article, profile)
    relevance_score, source_quality = apply_source_quality(result.score, article)
    if relevance_score < min_relevance:
        return None

    components = components_with_market_direction(
        article,
        profile,
        result.components,
    )
    components["source_quality"] = source_quality
    if candidate_component is not None:
        components["candidate_generation"] = candidate_component
    gate = category_news_gate(article, profile, components)
    components["category_gate"] = gate.as_score_component()
    if not gate.allowed:
        return None
    if score_context:
        components.update(score_context)
    return NewsLinkScore(relevance_score=relevance_score, components=components)
