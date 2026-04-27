"""Hybrid article-to-market relevance scoring.

This module is deliberately schema-free for v1: it improves candidate
generation without requiring a migration.  The main change is that we stop
asking only "does the headline mention this market?" and also ask
"does this headline match a factor that can move this market category?"

That matters for orthogonal catalysts.  Example: a crypto strike market can
move on "Trump appoints pro-digital-asset SEC chair" even if the headline does
not say "Bitcoin".
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from app.db.models import MarketNewsProfile, NewsArticle


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*")


@dataclass(frozen=True)
class RelevanceResult:
    """Scored article/profile match with explainable sub-components."""

    score: float
    components: dict


# Factor tags are intentionally broad.  They are not claims of causality; they
# are candidate-generation hints that get stored in score_components so a human
# can audit why an orthogonal article was linked.
CATEGORY_FACTOR_TERMS: dict[str, dict[str, tuple[str, ...]]] = {
    "crypto_strike": {
        "direct_crypto": (
            "bitcoin",
            "btc",
            "ether",
            "ethereum",
            "eth",
            "crypto",
            "cryptocurrency",
            "digital asset",
            "digital assets",
        ),
        "crypto_policy": (
            "sec",
            "cftc",
            "regulator",
            "regulatory",
            "regulation",
            "pro-crypto",
            "pro crypto",
            "digital currency",
            "digital currencies",
            "stablecoin",
            "stablecoins",
            "bitcoin etf",
            "spot bitcoin etf",
        ),
        "crypto_market_structure": (
            "coinbase",
            "binance",
            "kraken",
            "blackrock",
            "microstrategy",
            "etf inflows",
            "etf outflows",
            "exchange reserves",
        ),
        "macro_liquidity": (
            "fed",
            "federal reserve",
            "rate cut",
            "rate cuts",
            "rate hike",
            "rate hikes",
            "inflation",
            "cpi",
            "dollar",
            "treasury yields",
            "risk assets",
        ),
    },
    "macro": {
        "scheduled_macro": (
            "fomc",
            "federal reserve",
            "fed",
            "cpi",
            "inflation",
            "jobs report",
            "payrolls",
            "unemployment",
            "gdp",
            "pce",
            "treasury",
        ),
    },
    "corporate": {
        "corporate_event": (
            "earnings",
            "guidance",
            "merger",
            "acquisition",
            "takeover",
            "bankruptcy",
            "fda approval",
            "clinical trial",
            "sec filing",
        ),
    },
    "judicial": {
        "legal_event": (
            "supreme court",
            "scotus",
            "appeals court",
            "judge",
            "ruling",
            "injunction",
            "verdict",
            "lawsuit",
        ),
    },
    "election": {
        "political_event": (
            "poll",
            "endorsement",
            "primary",
            "campaign",
            "debate",
            "ballot",
            "drop out",
            "suspends campaign",
            "election",
        ),
    },
    "sports_outcome": {
        "sports_availability": (
            "injury",
            "injured",
            "questionable",
            "out",
            "lineup",
            "starter",
            "scratched",
            "suspension",
            "trade deadline",
        ),
    },
    "sports_derivative": {
        "sports_availability": (
            "injury",
            "injured",
            "questionable",
            "out",
            "lineup",
            "starter",
            "scratched",
            "weather delay",
        ),
    },
    "sports_prop": {
        "player_availability": (
            "injury",
            "injured",
            "questionable",
            "out",
            "minutes restriction",
            "lineup",
            "starter",
            "scratched",
        ),
    },
    "weather": {
        "weather_forecast": (
            "forecast",
            "storm",
            "hurricane",
            "tropical storm",
            "snow",
            "rain",
            "temperature",
            "heat wave",
            "cold front",
            "noaa",
            "national weather service",
        ),
    },
}

CATEGORY_PROFILE_ANCHORS: dict[str, tuple[str, ...]] = {
    "crypto_strike": (
        "bitcoin",
        "btc",
        "ether",
        "ethereum",
        "eth",
        "crypto",
        "cryptocurrency",
    ),
    "macro": (
        "fed",
        "fomc",
        "cpi",
        "inflation",
        "jobs",
        "payrolls",
        "unemployment",
        "gdp",
        "pce",
    ),
    "corporate": (
        "earnings",
        "guidance",
        "merger",
        "acquisition",
        "bankruptcy",
        "fda",
        "stock",
    ),
    "judicial": (
        "court",
        "scotus",
        "supreme",
        "judge",
        "ruling",
        "lawsuit",
        "legal",
        "appeals",
    ),
    "election": (
        "election",
        "primary",
        "campaign",
        "debate",
        "ballot",
        "poll",
    ),
    "sports_outcome": (
        "game",
        "match",
        "team",
        "winner",
        "league",
        "nba",
        "nfl",
        "mlb",
        "nhl",
        "tennis",
        "soccer",
    ),
    "sports_derivative": (
        "game",
        "match",
        "team",
        "score",
        "goals",
        "points",
        "tennis",
        "soccer",
    ),
    "sports_prop": (
        "player",
        "points",
        "rebounds",
        "assists",
        "goals",
        "yards",
    ),
    "weather": (
        "weather",
        "temperature",
        "rain",
        "snow",
        "storm",
        "hurricane",
        "noaa",
    ),
}


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _as_terms(values: Iterable[object] | None) -> set[str]:
    return {_norm(v) for v in (values or []) if _norm(v)}


def _article_text(article: NewsArticle) -> str:
    parts: list[str] = [
        article.title or "",
        article.summary or "",
        " ".join(str(x) for x in (article.keywords or [])),
        " ".join(str(x) for x in (article.entities or [])),
    ]
    return " ".join(parts).lower()


def _phrase_hits(text: str, terms: Iterable[str]) -> set[str]:
    hits: set[str] = set()
    padded = f" {text} "
    for term in terms:
        t = _norm(term)
        if not t:
            continue
        if " " in t:
            if t in text:
                hits.add(t)
        elif f" {t} " in padded:
            hits.add(t)
    return hits


def lexical_relevance(article: NewsArticle, profile: MarketNewsProfile) -> float:
    """Backwards-compatible direct lexical score.

    This keeps the existing behavior as a component, but also includes the
    summary because many providers expose useful context there even when the
    headline is terse.
    """

    text = _article_text(article)
    haystack = _tokens(text)
    needles = _as_terms(profile.normalized_keywords)
    if not needles:
        return 0.0

    token_needles = {n for n in needles if " " not in n}
    phrase_needles = {n for n in needles if " " in n}
    hits = (haystack & token_needles) | _phrase_hits(text, phrase_needles)
    return min(1.0, len(hits) / max(3, len(needles)))


def _entity_relevance(article: NewsArticle, profile: MarketNewsProfile) -> float:
    article_entities = _as_terms(article.entities)
    profile_entities = _as_terms(profile.entities)
    if not article_entities or not profile_entities:
        return 0.0
    return min(1.0, len(article_entities & profile_entities) / max(2, len(profile_entities)))


def _alias_relevance(article: NewsArticle, profile: MarketNewsProfile) -> float:
    aliases = _as_terms(profile.aliases)
    if not aliases:
        return 0.0
    return min(1.0, len(_phrase_hits(_article_text(article), aliases)) / max(1, len(aliases)))


def article_factor_hits(
    article: NewsArticle, profile: MarketNewsProfile
) -> dict[str, list[str]]:
    """Return category-specific factor hits for an article/profile pair."""

    category = _norm(profile.category)
    factors = CATEGORY_FACTOR_TERMS.get(category, {})
    if not factors:
        return {}

    text = _article_text(article)
    out: dict[str, list[str]] = {}
    for factor, terms in factors.items():
        hits = sorted(_phrase_hits(text, terms))
        if hits:
            out[factor] = hits
    return out


def profile_supports_category_factor(profile: MarketNewsProfile) -> bool:
    """True when the market profile has anchors for its broad factor category.

    This protects recall-oriented factor matching from classifier mistakes.
    A "Supreme Court" article should not link to every market accidentally
    labeled ``judicial`` unless the market text itself contains a legal anchor.
    """

    anchors = CATEGORY_PROFILE_ANCHORS.get(_norm(profile.category))
    if not anchors:
        return True
    text = " ".join(
        [
            " ".join(_as_terms(profile.normalized_keywords)),
            " ".join(_as_terms(profile.entities)),
            " ".join(_as_terms(profile.aliases)),
        ]
    )
    return bool(_phrase_hits(text, anchors))


def _factor_relevance(article: NewsArticle, profile: MarketNewsProfile) -> float:
    if not profile_supports_category_factor(profile):
        return 0.0

    hits = article_factor_hits(article, profile)
    if not hits:
        return 0.0

    matched_terms = [term for terms in hits.values() for term in terms]
    has_phrase = any(" " in term for term in matched_terms)

    # A single generic term like "SEC" or "Fed" should not by itself create a
    # strong link.  A phrase hit, multiple terms in one factor, or multiple
    # factor groups is stronger evidence of an orthogonal catalyst.
    if has_phrase or len(matched_terms) >= 2 or len(hits) >= 2:
        return 0.85
    return 0.25


def infer_direction_hint(article: NewsArticle, profile: MarketNewsProfile) -> str:
    """Cheap market-relative direction hint for score components.

    This is intentionally conservative.  The durable surveillance scorer should
    still decide "supports YES/NO" using market-specific contract wording.
    """

    text = _article_text(article)
    category = _norm(profile.category)

    if category == "crypto_strike":
        bullish = (
            "pro-crypto",
            "pro crypto",
            "approval",
            "approves",
            "rate cut",
            "rate cuts",
            "etf inflows",
            "record inflows",
            "friendlier",
        )
        bearish = (
            "crackdown",
            "ban",
            "rejects",
            "rejection",
            "lawsuit",
            "rate hike",
            "rate hikes",
            "outflows",
        )
        if _phrase_hits(text, bullish):
            return "bullish_underlier"
        if _phrase_hits(text, bearish):
            return "bearish_underlier"

    return "unknown"


def hybrid_news_relevance(
    article: NewsArticle, profile: MarketNewsProfile
) -> RelevanceResult:
    """Score direct + orthogonal relevance with auditable components."""

    lexical = lexical_relevance(article, profile)
    entity = _entity_relevance(article, profile)
    alias = _alias_relevance(article, profile)
    factor = _factor_relevance(article, profile)
    factor_hits = article_factor_hits(article, profile)

    score = min(
        1.0,
        0.35 * lexical
        + 0.15 * entity
        + 0.05 * alias
        + 0.45 * factor,
    )

    return RelevanceResult(
        score=score,
        components={
            "lexical_relevance": lexical,
            "entity_relevance": entity,
            "alias_relevance": alias,
            "factor_relevance": factor,
            "factor_hits": factor_hits,
            "direction_hint": infer_direction_hint(article, profile),
            "scorer": "hybrid_news_relevance_v1",
        },
    )
