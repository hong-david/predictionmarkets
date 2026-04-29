"""Lightweight in-memory index for market news profiles."""

from __future__ import annotations

from dataclasses import dataclass
import re

from app.db.models import MarketNewsProfile, NewsArticle
from app.services.news_relevance import CATEGORY_FACTOR_TERMS

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_ORIENTATION_MARKERS = {"above_threshold", "below_threshold"}
_GENERIC_TERMS = {
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
    "is",
    "least",
    "less",
    "market",
    "match",
    "million",
    "new",
    "of",
    "on",
    "or",
    "over",
    "q1",
    "q2",
    "q3",
    "q4",
    "report",
    "the",
    "to",
    "total",
    "under",
    "vs",
    "what",
    "when",
    "which",
    "who",
    "will",
    "with",
    "year",
}
_ALLOWED_SHORT_TERMS = {
    "ai",
    "btc",
    "eth",
    "sec",
    "fed",
    "cpi",
    "pce",
    "gdp",
    "wti",
    "fbi",
    "doj",
    "nba",
    "nfl",
    "mlb",
    "nhl",
    "ufc",
}


@dataclass(frozen=True)
class NewsProfileIndex:
    profiles: list[MarketNewsProfile]
    term_index: dict[str, list[MarketNewsProfile]]
    category_index: dict[str, list[MarketNewsProfile]]


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text))


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


def _indexable_term(value: object) -> str | None:
    term = _norm(value)
    if (
        not term
        or term in _ORIENTATION_MARKERS
        or term in _GENERIC_TERMS
        or term.startswith("kx")
        or term.replace(".", "", 1).isdigit()
        or (len(term) <= 2 and term not in _ALLOWED_SHORT_TERMS)
    ):
        return None
    return term


def _profile_index_terms(profile: MarketNewsProfile) -> set[str]:
    terms: set[str] = set()
    for value in (
        list(profile.normalized_keywords or [])
        + list(profile.entities or [])
        + list(profile.aliases or [])
    ):
        term = _indexable_term(value)
        if term is None:
            continue
        if " " in term:
            terms.update(_tokens(term))
        else:
            terms.add(term)
    return terms


def _has_factor_term(text: str, category: str) -> bool:
    factors = CATEGORY_FACTOR_TERMS.get(category, {})
    padded = f" {text} "
    for terms in factors.values():
        for term in terms:
            cleaned = _norm(term)
            if not cleaned:
                continue
            if " " in cleaned and cleaned in text:
                return True
            if " " not in cleaned and f" {cleaned} " in padded:
                return True
    return False


def build_news_profile_index(profiles: list[MarketNewsProfile]) -> NewsProfileIndex:
    term_index: dict[str, list[MarketNewsProfile]] = {}
    category_index: dict[str, list[MarketNewsProfile]] = {}
    for profile in profiles:
        category_index.setdefault(_norm(profile.category) or "unknown", []).append(
            profile
        )
        for term in _profile_index_terms(profile):
            term_index.setdefault(term, []).append(profile)
    return NewsProfileIndex(
        profiles=profiles,
        term_index=term_index,
        category_index=category_index,
    )


def candidate_pool_for_article(
    article: NewsArticle,
    index: NewsProfileIndex,
    *,
    max_factor_profiles_per_category: int = 5000,
) -> list[MarketNewsProfile]:
    text = _article_text(article)
    seen: dict[int, MarketNewsProfile] = {}
    for token in _tokens(text):
        for profile in index.term_index.get(token, ()):
            seen[int(profile.market_pk)] = profile
    for category in ("crypto_strike", "macro", "weather"):
        if _has_factor_term(text, category):
            for profile in index.category_index.get(category, ())[
                :max_factor_profiles_per_category
            ]:
                seen[int(profile.market_pk)] = profile
    return list(seen.values())
