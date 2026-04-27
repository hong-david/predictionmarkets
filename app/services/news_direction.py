"""Market-relative direction scoring for linked news.

The relevance layer can say "this article is a crypto catalyst" and may add a
generic underlier hint such as ``bullish_underlier``. This module maps that
generic hint onto the contract wording: bullish is YES for "above" markets, but
NO for "below" markets.

This is intentionally schema-free. It uses the existing ``MarketNewsProfile``
keywords/aliases and the relevance score components already produced during
news linking.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from app.db.models import MarketNewsProfile, NewsArticle

DirectionLabel = Literal["supports_yes", "supports_no", "ambiguous", "unrelated"]
MarketOrientation = Literal[
    "above_threshold",
    "below_threshold",
    "unknown_orientation",
]
UnderlierDirection = Literal[
    "bullish_underlier",
    "bearish_underlier",
    "neutral_underlier",
    "conflicting_underlier",
    "unknown_underlier",
]

_NON_WORD_RE = re.compile(r"[^a-z0-9]+")

_ABOVE_TERMS: tuple[str, ...] = (
    "above",
    "over",
    "greater than",
    "at least",
    "exceed",
    "exceeds",
    "exceeded",
    "exceeding",
    "cross",
    "crosses",
    "crossed",
    "reach",
    "reaches",
    "reached",
    "trade above",
    "trades above",
    "trading above",
    "finish above",
    "close above",
    "settle above",
)

_BELOW_TERMS: tuple[str, ...] = (
    "below",
    "under",
    "less than",
    "at most",
    "fall below",
    "falls below",
    "fell below",
    "drop below",
    "drops below",
    "dropped below",
    "trade below",
    "trades below",
    "trading below",
    "finish below",
    "close below",
    "settle below",
)

_BULLISH_TERMS: tuple[str, ...] = (
    "pro crypto",
    "pro digital currency",
    "pro digital asset",
    "friendlier",
    "friendlier stance",
    "friendly",
    "supportive",
    "approval",
    "approves",
    "approved",
    "rate cut",
    "rate cuts",
    "etf inflows",
    "record inflows",
    "rally",
    "rallies",
    "rallied",
    "surge",
    "surges",
    "surged",
    "adoption",
)

_BEARISH_TERMS: tuple[str, ...] = (
    "crackdown",
    "ban",
    "bans",
    "banned",
    "rejects",
    "rejection",
    "lawsuit",
    "rate hike",
    "rate hikes",
    "etf outflows",
    "outflows",
    "selloff",
    "sell off",
    "falls",
    "fell",
    "drops",
    "dropped",
    "bearish",
    "hack",
    "probe",
    "investigation",
)


@dataclass(frozen=True)
class MarketDirectionResult:
    label: DirectionLabel
    confidence: float
    market_orientation: MarketOrientation
    underlier_direction: UnderlierDirection
    evidence_terms: tuple[str, ...]
    rationale: str

    def as_score_component(self) -> dict:
        return {
            "label": self.label,
            "confidence": self.confidence,
            "market_orientation": self.market_orientation,
            "underlier_direction": self.underlier_direction,
            "evidence_terms": list(self.evidence_terms),
            "rationale": self.rationale,
        }


def _normalize_text(*parts: object) -> str:
    raw = " ".join(str(p or "") for p in parts)
    return f" {_NON_WORD_RE.sub(' ', raw.lower()).strip()} "


def _term_hits(normalized_text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    hits: list[str] = []
    for term in terms:
        normalized_term = _normalize_text(term).strip()
        if normalized_term and f" {normalized_term} " in normalized_text:
            hits.append(term)
    return tuple(dict.fromkeys(hits))


def _profile_text(profile: MarketNewsProfile) -> str:
    return _normalize_text(
        " ".join(str(x) for x in (profile.normalized_keywords or [])),
        " ".join(str(x) for x in (profile.aliases or [])),
        profile.category,
    )


def infer_market_orientation_from_text(text: str) -> MarketOrientation:
    normalized = _normalize_text(text)
    if " above_threshold " in normalized and " below_threshold " not in normalized:
        return "above_threshold"
    if " below_threshold " in normalized and " above_threshold " not in normalized:
        return "below_threshold"
    above_hits = _term_hits(normalized, _ABOVE_TERMS)
    below_hits = _term_hits(normalized, _BELOW_TERMS)
    if above_hits and not below_hits:
        return "above_threshold"
    if below_hits and not above_hits:
        return "below_threshold"
    return "unknown_orientation"


def orientation_keywords_for_market_text(text: str) -> tuple[str, ...]:
    orientation = infer_market_orientation_from_text(text)
    if orientation == "unknown_orientation":
        return ()
    return (orientation,)


def _article_text(article: NewsArticle) -> str:
    return _normalize_text(
        article.title,
        article.summary,
        " ".join(str(x) for x in (article.keywords or [])),
        " ".join(str(x) for x in (article.entities or [])),
    )


def infer_market_orientation(profile: MarketNewsProfile) -> MarketOrientation:
    return infer_market_orientation_from_text(_profile_text(profile))


def _flatten_factor_terms(components: dict | None) -> tuple[str, ...]:
    if not components:
        return ()
    hits = components.get("factor_hits")
    if not isinstance(hits, dict):
        return ()
    out: list[str] = []
    for terms in hits.values():
        if isinstance(terms, list):
            out.extend(str(term) for term in terms if str(term).strip())
    return tuple(dict.fromkeys(out))


def _infer_underlier_direction(
    article: NewsArticle,
    profile: MarketNewsProfile,
    components: dict | None,
) -> tuple[UnderlierDirection, tuple[str, ...]]:
    text = _article_text(article)
    factor_text = _normalize_text(*_flatten_factor_terms(components))
    combined_text = f"{text} {factor_text}"

    direction_hint = str((components or {}).get("direction_hint") or "")
    if str(profile.category or "").lower() == "crypto_strike":
        bullish_hits = _term_hits(combined_text, _BULLISH_TERMS)
        bearish_hits = _term_hits(combined_text, _BEARISH_TERMS)
    else:
        bullish_hits = ()
        bearish_hits = ()

    if direction_hint == "bullish_underlier":
        bullish_hits = tuple(dict.fromkeys((*bullish_hits, direction_hint)))
    elif direction_hint == "bearish_underlier":
        bearish_hits = tuple(dict.fromkeys((*bearish_hits, direction_hint)))

    if bullish_hits and bearish_hits:
        return "conflicting_underlier", tuple(
            dict.fromkeys((*bullish_hits, *bearish_hits))
        )
    if bullish_hits:
        return "bullish_underlier", bullish_hits
    if bearish_hits:
        return "bearish_underlier", bearish_hits
    if direction_hint in {"neutral_underlier", "neutral"}:
        return "neutral_underlier", ()
    return "unknown_underlier", ()


def _looks_unrelated(components: dict | None) -> bool:
    if not components:
        return False
    numeric_keys = (
        "lexical_relevance",
        "entity_relevance",
        "alias_relevance",
        "factor_relevance",
    )
    if any(float(components.get(key) or 0.0) > 0.0 for key in numeric_keys):
        return False
    return not bool(components.get("factor_hits"))


def score_market_direction(
    article: NewsArticle,
    profile: MarketNewsProfile,
    components: dict | None = None,
) -> MarketDirectionResult:
    """Map generic article direction onto the YES/NO side of a market."""

    if _looks_unrelated(components):
        return MarketDirectionResult(
            label="unrelated",
            confidence=0.0,
            market_orientation="unknown_orientation",
            underlier_direction="unknown_underlier",
            evidence_terms=(),
            rationale="relevance components did not match this market",
        )

    market_orientation = infer_market_orientation(profile)
    underlier_direction, evidence_terms = _infer_underlier_direction(
        article, profile, components
    )

    if market_orientation == "unknown_orientation" or underlier_direction in {
        "unknown_underlier",
        "neutral_underlier",
        "conflicting_underlier",
    }:
        return MarketDirectionResult(
            label="ambiguous",
            confidence=0.25 if evidence_terms else 0.15,
            market_orientation=market_orientation,
            underlier_direction=underlier_direction,
            evidence_terms=evidence_terms,
            rationale="direction or market orientation was not clear enough",
        )

    mapping: dict[tuple[MarketOrientation, UnderlierDirection], DirectionLabel] = {
        ("above_threshold", "bullish_underlier"): "supports_yes",
        ("above_threshold", "bearish_underlier"): "supports_no",
        ("below_threshold", "bullish_underlier"): "supports_no",
        ("below_threshold", "bearish_underlier"): "supports_yes",
    }
    label = mapping.get((market_orientation, underlier_direction), "ambiguous")
    confidence = 0.65
    if len(evidence_terms) >= 2:
        confidence += 0.1
    if str((components or {}).get("direction_hint") or "") == underlier_direction:
        confidence += 0.05

    return MarketDirectionResult(
        label=label,
        confidence=round(min(confidence, 0.85), 3),
        market_orientation=market_orientation,
        underlier_direction=underlier_direction,
        evidence_terms=evidence_terms,
        rationale="underlier direction mapped onto market threshold wording",
    )


def components_with_market_direction(
    article: NewsArticle,
    profile: MarketNewsProfile,
    components: dict,
) -> dict:
    """Return score components with a JSON-friendly market_direction block."""

    out = dict(components)
    out["market_direction"] = score_market_direction(
        article,
        profile,
        components,
    ).as_score_component()
    return out
