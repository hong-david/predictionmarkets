"""Global-first news correlation helpers.

The scraper should ingest articles once, normalize them, candidate-generate
markets from precomputed profiles, and only promote durable evidence when
timing plus market movement looks suspicious.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re
from urllib.parse import urlparse

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Market, MarketNewsProfile, NewsArticle, NewsEvent
from app.services.news_direction import orientation_keywords_for_market_text


@dataclass(frozen=True)
class NormalizedArticle:
    canonical_url: str
    title: str
    published_at: datetime | None = None
    first_seen_at: datetime | None = None
    summary: str | None = None
    language: str | None = None
    entities: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    source_tier: str | None = None


# News-linking scope is intentionally broader than raw retention scope.  Raw
# Kalshi tape can be dropped for low-value categories while the cheap
# article-to-market profile still exists for correlation and market detail.
NEWS_PROFILE_EXCLUDED_CATEGORIES: frozenset[str] = frozenset({"exotic_combo"})
NEWS_PROFILE_EXCLUDED_STATUSES: frozenset[str] = frozenset({"unknown"})
_NEWS_TERM_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*", re.IGNORECASE)
_NEWS_GENERIC_TERMS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "above",
        "be",
        "before",
        "below",
        "billion",
        "by",
        "company",
        "companies",
        "corp",
        "corporation",
        "earnings",
        "during",
        "director",
        "end",
        "event",
        "for",
        "from",
        "greater",
        "how",
        "in",
        "inc",
        "international",
        "is",
        "its",
        "least",
        "less",
        "leave",
        "leaves",
        "llc",
        "ltd",
        "many",
        "market",
        "million",
        "most",
        "new",
        "not",
        "of",
        "on",
        "or",
        "over",
        "plc",
        "press",
        "price",
        "q1",
        "q2",
        "q3",
        "q4",
        "qualify",
        "quarter",
        "report",
        "reported",
        "reports",
        "say",
        "says",
        "thousand",
        "the",
        "this",
        "to",
        "total",
        "trade",
        "trades",
        "trading",
        "under",
        "vs",
        "what",
        "when",
        "which",
        "who",
        "will",
        "with",
        "year",
        "above_threshold",
        "below_threshold",
    }
)
_NEWS_STRUCTURAL_CLASSIFIER_TAGS: frozenset[str] = frozenset(
    {
        "daily",
        "earnings",
        "market_structure",
        "merger",
        "player_prop",
        "public_underlying",
        "scheduled_announcement",
        "short_window",
        "single_actor_leverage",
    }
)
_NEWS_DATE_TERMS: frozenset[str] = frozenset(
    {
        "jan",
        "january",
        "feb",
        "february",
        "mar",
        "march",
        "apr",
        "april",
        "may",
        "jun",
        "june",
        "jul",
        "july",
        "aug",
        "august",
        "sep",
        "sept",
        "september",
        "oct",
        "october",
        "nov",
        "november",
        "dec",
        "december",
    }
)
_ALLOWED_SHORT_NEWS_TERMS: frozenset[str] = frozenset(
    {
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
        "fbi",
        "doj",
        "nba",
        "nfl",
        "mlb",
        "nhl",
        "ufc",
    }
)

_ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("bitcoin", "btc", "crypto", "digital asset", "digital assets"),
    ("ethereum", "ether", "eth", "crypto", "digital asset", "digital assets"),
    ("solana", "sol", "crypto", "digital asset", "digital assets"),
    ("dogecoin", "doge", "crypto", "digital asset", "digital assets"),
    ("xrp", "ripple", "crypto", "digital asset", "digital assets"),
    ("bnb", "binance", "crypto", "digital asset", "digital assets"),
    ("oil", "crude", "wti", "brent", "gasoline", "energy"),
    ("gas", "gasoline", "fuel", "energy"),
    ("gold", "bullion", "precious metal", "precious metals"),
    ("silver", "precious metal", "precious metals"),
    ("fed", "fomc", "federal reserve", "interest rate", "rates"),
    ("cpi", "inflation", "consumer prices", "prices"),
    ("pce", "inflation", "consumer prices", "prices"),
    ("jobs", "payrolls", "nfp", "employment", "unemployment"),
    ("gdp", "growth", "economy", "economic growth"),
    ("temperature", "heat", "cold", "weather", "forecast"),
    ("rain", "precipitation", "weather", "forecast"),
    ("snow", "weather", "forecast", "storm"),
)


def canonical_url_hash(url: str) -> str:
    return sha256(url.strip().lower().encode("utf-8")).hexdigest()


def upsert_article(db: Session, article: NormalizedArticle) -> int:
    parsed = urlparse(article.canonical_url)
    values = {
        "canonical_url_hash": canonical_url_hash(article.canonical_url),
        "canonical_url": article.canonical_url,
        "domain": parsed.netloc.lower() or None,
        "source_tier": article.source_tier,
        "published_at": article.published_at,
        "first_seen_at": article.first_seen_at or datetime.now(timezone.utc),
        "title": article.title,
        "summary": article.summary,
        "language": article.language,
        "entities": list(article.entities),
        "keywords": list(article.keywords),
    }
    stmt = pg_insert(NewsArticle).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["canonical_url_hash"],
        set_={
            "canonical_url": stmt.excluded.canonical_url,
            "domain": stmt.excluded.domain,
            "source_tier": stmt.excluded.source_tier,
            "published_at": stmt.excluded.published_at,
            "title": stmt.excluded.title,
            "summary": stmt.excluded.summary,
            "language": stmt.excluded.language,
            "entities": stmt.excluded.entities,
            "keywords": stmt.excluded.keywords,
        },
    ).returning(NewsArticle.id)
    return int(db.execute(stmt).scalar_one())


def is_news_profile_candidate(market: Market) -> bool:
    """Return True when a market deserves cheap news-linking metadata.

    This deliberately does *not* mirror retention scope.  For example, a
    crypto strike can be too noisy/large for raw tape retention but still be a
    valid target for a Bitcoin article in the market detail view.
    """

    title = (market.title or "").strip()
    if not title or title == (market.market_id or "").strip():
        return False
    if (market.status or "").lower() in NEWS_PROFILE_EXCLUDED_STATUSES:
        return False
    if (market.category or "").lower() in NEWS_PROFILE_EXCLUDED_CATEGORIES:
        return False
    return True


def _trusted_classifier_tags(market: Market) -> list[str]:
    """Use classifier tags as news anchors only when the verdict is reliable."""

    trusted = (market.classifier_confidence or "").lower() == "high" or (
        market.classifier_layer or ""
    ).lower() in {"prefix_rule", "kalshi_taxonomy"}
    if trusted:
        tags = [str(tag) for tag in (market.classifier_tags or [])]
    else:
        return []
    return [
        tag
        for tag in tags
        if tag.strip().lower() not in _NEWS_STRUCTURAL_CLASSIFIER_TAGS
    ]


def _alias_terms_for_market(market: Market, text: str) -> list[str]:
    haystack = " ".join(
        str(part or "").lower()
        for part in (
            text,
            market.market_id,
            market.event_id,
            market.ticker,
            " ".join(_trusted_classifier_tags(market)),
        )
    )
    terms: list[str] = []
    for group in _ALIAS_GROUPS:
        if any(f" {term} " in f" {haystack} " for term in group):
            terms.extend(group)
    return terms


def _clean_news_terms(text: str, *, max_terms: int = 16) -> list[str]:
    out: list[str] = []
    for raw in _NEWS_TERM_RE.findall(text or ""):
        term = raw.strip().lower()
        if not term:
            continue
        if term.startswith("kx"):
            continue
        if term.isdigit() or term.replace(".", "", 1).isdigit():
            continue
        if term in _NEWS_GENERIC_TERMS or term in _NEWS_DATE_TERMS:
            continue
        if len(term) <= 2 and term not in _ALLOWED_SHORT_NEWS_TERMS:
            continue
        if term not in out:
            out.append(term)
        if len(out) >= max_terms:
            break
    return out


def market_news_search_query(market: Market, *, max_terms: int = 8) -> str:
    """Compact search text for related-news discovery on a market page.

    Full market titles contain dates, threshold words, and exchange ticker
    fragments that are useful for display but harmful for broad news search.
    This keeps the high-signal anchors: people, organizations, underliers,
    teams, offices, and topic nouns.
    """

    text = " ".join(p for p in (market.title, market.subtitle) if p)
    terms = _clean_news_terms(text, max_terms=max_terms)
    return " ".join(terms) or (market.title or market.market_id or "")


def profile_for_market(market: Market) -> dict:
    text = " ".join(p for p in (market.title, market.subtitle, market.market_id) if p)
    tokens = _clean_news_terms(text, max_terms=16)
    tokens.extend(orientation_keywords_for_market_text(text))
    tokens.extend(_trusted_classifier_tags(market))
    tokens.extend(_alias_terms_for_market(market, text))
    aliases = [market.market_id]
    if market.event_id:
        aliases.append(market.event_id)
    return {
        "market_pk": market.id,
        "normalized_keywords": sorted(set(tokens)),
        "entities": [],
        "aliases": aliases,
        "category": market.category,
        "active_from": market.open_time,
        "active_to": market.close_time,
    }


def upsert_market_news_profile(db: Session, market: Market) -> None:
    values = profile_for_market(market)
    stmt = pg_insert(MarketNewsProfile).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["market_pk"],
        set_={
            "normalized_keywords": stmt.excluded.normalized_keywords,
            "entities": stmt.excluded.entities,
            "aliases": stmt.excluded.aliases,
            "category": stmt.excluded.category,
            "active_from": stmt.excluded.active_from,
            "active_to": stmt.excluded.active_to,
        },
    )
    db.execute(stmt)


def record_news_market_candidate(
    db: Session,
    *,
    article_id: int,
    market_pk: int,
    relevance_score: float,
    pre_news_trade_score: float = 0.0,
    leakage_window_seconds: int | None = None,
    score_components: dict | None = None,
) -> None:
    stmt = pg_insert(NewsEvent).values(
        article_id=article_id,
        market_pk=market_pk,
        relevance_score=relevance_score,
        pre_news_trade_score=pre_news_trade_score,
        leakage_window_seconds=leakage_window_seconds,
        score_components=score_components or {},
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_news_events_article_market",
        set_={
            "relevance_score": stmt.excluded.relevance_score,
            "pre_news_trade_score": stmt.excluded.pre_news_trade_score,
            "leakage_window_seconds": stmt.excluded.leakage_window_seconds,
            "score_components": stmt.excluded.score_components,
        },
    )
    db.execute(stmt)
