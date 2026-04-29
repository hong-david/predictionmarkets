"""Category-aware guardrails for article-to-market news links.

The hybrid relevance scorer is intentionally recall-oriented. This gate is the
precision layer: it prevents broad factor-only articles from attaching to
specific company, person, team, or other narrow markets unless the article has
a direct anchor for that market.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.models import MarketNewsProfile, NewsArticle

DIRECT_REASON_NAMES = {"keyword_overlap", "alias_overlap", "entity_overlap"}
DIRECT_SCORE_KEYS = ("lexical_relevance", "entity_relevance", "alias_relevance")
DIRECT_REQUIRED_CATEGORIES = {
    "corporate",
    "election",
    "judicial",
    "politics",
    "sports_outcome",
    "sports_derivative",
    "sports_prop",
    "other",
}
FACTOR_ALLOWED_CATEGORIES = {"crypto_strike", "macro", "weather"}


@dataclass(frozen=True)
class NewsCategoryGateDecision:
    allowed: bool
    reason: str
    category: str
    direct_anchor: bool
    factor_only: bool

    def as_score_component(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "category": self.category,
            "direct_anchor": self.direct_anchor,
            "factor_only": self.factor_only,
            "scorer": "news_category_gate_v1",
        }


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _candidate_reasons(components: dict[str, Any]) -> set[str]:
    candidate = components.get("candidate_generation")
    if not isinstance(candidate, dict):
        return set()
    reasons = candidate.get("candidate_reasons")
    if not isinstance(reasons, list):
        return set()
    return {_norm(reason) for reason in reasons}


def _has_direct_anchor(components: dict[str, Any]) -> bool:
    if any(float(components.get(key) or 0.0) > 0.0 for key in DIRECT_SCORE_KEYS):
        return True
    return bool(_candidate_reasons(components) & DIRECT_REASON_NAMES)


def _is_factor_only(components: dict[str, Any], *, direct_anchor: bool) -> bool:
    return (
        not direct_anchor
        and float(components.get("factor_relevance") or 0.0) > 0.0
        and bool(components.get("factor_hits") or {})
    )


def category_news_gate(
    article: NewsArticle,
    profile: MarketNewsProfile,
    components: dict[str, Any],
) -> NewsCategoryGateDecision:
    """Decide whether a scored article/profile link is precise enough to keep."""

    del article  # reserved for future source/body-specific gates
    category = _norm(profile.category) or "unknown"
    direct_anchor = _has_direct_anchor(components)
    factor_only = _is_factor_only(components, direct_anchor=direct_anchor)

    if category in DIRECT_REQUIRED_CATEGORIES and not direct_anchor:
        return NewsCategoryGateDecision(
            allowed=False,
            reason=f"{category}_requires_direct_market_anchor",
            category=category,
            direct_anchor=direct_anchor,
            factor_only=factor_only,
        )

    if category not in FACTOR_ALLOWED_CATEGORIES and factor_only:
        return NewsCategoryGateDecision(
            allowed=False,
            reason="factor_only_not_allowed_for_category",
            category=category,
            direct_anchor=direct_anchor,
            factor_only=factor_only,
        )

    if category == "weather" and factor_only:
        source_quality = components.get("source_quality")
        authority = (
            _norm(source_quality.get("authority_tier"))
            if isinstance(source_quality, dict)
            else ""
        )
        if authority != "official":
            return NewsCategoryGateDecision(
                allowed=False,
                reason="weather_factor_requires_official_source_or_direct_anchor",
                category=category,
                direct_anchor=direct_anchor,
                factor_only=factor_only,
            )

    return NewsCategoryGateDecision(
        allowed=True,
        reason="accepted",
        category=category,
        direct_anchor=direct_anchor,
        factor_only=factor_only,
    )
