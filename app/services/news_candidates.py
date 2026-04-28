"""Schema-free candidate generation for article-to-market linking.

The hybrid relevance scorer is explainable but still more expensive than a
first-pass filter. This module narrows an article to likely market profiles
using cheap lexical, alias, entity, and category-factor hints before the
ingestor runs full relevance + market-direction scoring.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from app.db.models import MarketNewsProfile, NewsArticle
from app.services.news_relevance import (
    CATEGORY_FACTOR_TERMS,
    profile_supports_factor,
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_ORIENTATION_MARKERS = {"above_threshold", "below_threshold"}
_ALLOWED_SHORT_TERMS = {
    "ai",
    "btc",
    "eth",
    "sol",
    "xrp",
    "bnb",
    "sec",
    "fed",
    "cpi",
    "pce",
    "gdp",
    "nfp",
    "wti",
    "oil",
    "fda",
    "nba",
    "nfl",
    "mlb",
    "nhl",
    "ufc",
}
_GENERIC_PROFILE_TERMS = {
    "a",
    "above",
    "an",
    "and",
    "are",
    "at",
    "be",
    "before",
    "below",
    "billion",
    "by",
    "company",
    "companies",
    "corp",
    "corporation",
    "during",
    "end",
    "event",
    "for",
    "from",
    "game",
    "greater",
    "how",
    "in",
    "inc",
    "international",
    "is",
    "its",
    "least",
    "league",
    "less",
    "llc",
    "ltd",
    "market",
    "match",
    "many",
    "merger",
    "million",
    "most",
    "new",
    "not",
    "of",
    "on",
    "or",
    "over",
    "plc",
    "point",
    "q1",
    "q2",
    "q3",
    "q4",
    "qualify",
    "quarter",
    "report",
    "reported",
    "reports",
    "round",
    "say",
    "says",
    "start",
    "thousand",
    "the",
    "this",
    "to",
    "total",
    "under",
    "vs",
    "what",
    "when",
    "which",
    "who",
    "will",
    "win",
    "winner",
    "with",
    "year",
}


@dataclass(frozen=True)
class NewsMarketCandidate:
    profile: MarketNewsProfile
    score: float
    reasons: tuple[str, ...]
    matched_terms: tuple[str, ...]

    def as_score_component(self) -> dict:
        return {
            "candidate_score": round(self.score, 3),
            "candidate_reasons": list(self.reasons),
            "matched_terms": list(self.matched_terms[:20]),
        }


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _article_text(article: NewsArticle) -> str:
    return " ".join(
        str(part or "")
        for part in (
            article.title,
            article.summary,
            " ".join(str(x) for x in (article.keywords or [])),
            " ".join(str(x) for x in (article.entities or [])),
        )
    ).lower()


def _phrase_hits(text: str, terms: Iterable[str]) -> tuple[str, ...]:
    hits: list[str] = []
    padded = f" {text} "
    for term in terms:
        t = _norm(term)
        if not t:
            continue
        if " " in t:
            if t in text:
                hits.append(t)
        elif f" {t} " in padded:
            hits.append(t)
    return tuple(dict.fromkeys(hits))


def _as_terms(values: Iterable[object] | None) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            term
            for value in (values or [])
            if (term := _norm(value))
            and term not in _ORIENTATION_MARKERS
            and term not in _GENERIC_PROFILE_TERMS
            and not term.replace(".", "", 1).isdigit()
            and (len(term) > 2 or term in _ALLOWED_SHORT_TERMS)
        )
    )


def _strong_factor_hits_for_category(
    article_text: str,
    profile: MarketNewsProfile,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    factors = CATEGORY_FACTOR_TERMS.get(_norm(profile.category), {})
    if not factors:
        return ()

    hits_by_factor: list[tuple[str, tuple[str, ...]]] = []
    for factor, terms in factors.items():
        if not profile_supports_factor(profile, factor):
            continue
        hits = _phrase_hits(article_text, terms)
        if hits:
            hits_by_factor.append((factor, hits))

    matched_terms = [term for _factor, terms in hits_by_factor for term in terms]
    has_phrase = any(" " in term for term in matched_terms)
    if has_phrase or len(matched_terms) >= 2 or len(hits_by_factor) >= 2:
        return tuple(hits_by_factor)
    return ()


def news_market_candidates(
    article: NewsArticle,
    profiles: list[MarketNewsProfile],
    *,
    max_candidates: int | None = None,
) -> list[NewsMarketCandidate]:
    """Return likely market profiles to pass into the full relevance scorer."""

    text = _article_text(article)
    article_tokens = _tokens(text)
    article_entities = {_norm(x) for x in (article.entities or []) if _norm(x)}

    candidates: list[NewsMarketCandidate] = []
    for profile in profiles:
        score = 0.0
        reasons: list[str] = []
        matched_terms: list[str] = []

        keywords = _as_terms(profile.normalized_keywords)
        token_keywords = {kw for kw in keywords if " " not in kw}
        phrase_keywords = tuple(kw for kw in keywords if " " in kw)
        keyword_hits = sorted((article_tokens & token_keywords) | set(_phrase_hits(text, phrase_keywords)))
        if keyword_hits:
            score += min(3.0, 0.75 * len(keyword_hits))
            reasons.append("keyword_overlap")
            matched_terms.extend(keyword_hits)

        aliases = _as_terms(profile.aliases)
        alias_hits = _phrase_hits(text, aliases)
        if alias_hits:
            score += 2.0
            reasons.append("alias_overlap")
            matched_terms.extend(alias_hits)

        profile_entities = {_norm(x) for x in (profile.entities or []) if _norm(x)}
        entity_hits = sorted(article_entities & profile_entities)
        if entity_hits:
            score += min(2.0, len(entity_hits))
            reasons.append("entity_overlap")
            matched_terms.extend(entity_hits)

        factor_hits = _strong_factor_hits_for_category(text, profile)
        if (
            factor_hits
            and _norm(profile.category) == "corporate"
            and not keyword_hits
            and not alias_hits
            and not entity_hits
        ):
            factor_hits = ()
        if factor_hits:
            score += 2.5
            reasons.append("category_factor")
            for factor, terms in factor_hits:
                matched_terms.append(factor)
                matched_terms.extend(terms)

        if score <= 0:
            continue

        candidates.append(
            NewsMarketCandidate(
                profile=profile,
                score=score,
                reasons=tuple(dict.fromkeys(reasons)),
                matched_terms=tuple(dict.fromkeys(matched_terms)),
            )
        )

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    if max_candidates is not None:
        return candidates[: max(0, max_candidates)]
    return candidates
