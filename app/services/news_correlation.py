"""Global-first news correlation helpers.

The scraper should ingest articles once, normalize them, candidate-generate
markets from precomputed profiles, and only promote durable evidence when
timing plus market movement looks suspicious.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlparse

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models import Market, MarketNewsProfile, NewsArticle, NewsEvent
from app.services.news_gdelt import tokenize_for_gdelt


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


def profile_for_market(market: Market) -> dict:
    text = " ".join(p for p in (market.title, market.subtitle, market.market_id) if p)
    tokens = tokenize_for_gdelt(text, max_tokens=16).split()
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


def lexical_relevance(article: NewsArticle, profile: MarketNewsProfile) -> float:
    haystack = {
        str(x).lower()
        for x in ((article.keywords or []) + (article.entities or []))
        if str(x).strip()
    }
    haystack.update((article.title or "").lower().split())
    needles = {str(x).lower() for x in (profile.normalized_keywords or [])}
    if not needles:
        return 0.0
    return min(1.0, len(haystack & needles) / max(3, len(needles)))


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
