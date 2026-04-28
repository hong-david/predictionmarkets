"""Dashboard search over markets and news.

OpenSearch is the fast path for fuzzy, prefix, and cross-document search. The
Postgres fallback keeps the product useful during local development or when the
search index has not been built yet.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import logging
import re
import time
from typing import Any, Iterable, Literal

import httpx
from sqlalchemy import func, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    Market,
    MarketMetric,
    MarketNewsProfile,
    NewsArticle,
    NewsEvent,
)
from app.services.news_candidates import news_market_candidates
from app.services.news_correlation import profile_for_market
from app.services.surveillance_scores import (
    evidence_score_0_100,
    market_priority_value,
    prior_rank,
    urgency_score_0_100,
)

logger = logging.getLogger(__name__)

SearchScope = Literal["all", "markets", "news"]

MARKET_INDEX_SUFFIX = "markets"
NEWS_INDEX_SUFFIX = "news"
MAX_SEARCH_LIMIT = 25
MIN_QUERY_CHARS = 2
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*", re.IGNORECASE)
_GENERIC_SEARCH_TERMS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "above",
    "after",
    "before",
    "below",
    "by",
    "director",
    "event",
    "for",
    "from",
    "in",
    "is",
    "leave",
    "leaves",
    "market",
    "of",
    "on",
    "or",
    "price",
    "resolve",
    "the",
    "to",
    "trade",
    "trades",
    "trading",
    "will",
    "with",
    "year",
}
_ANCHOR_SEARCH_TERMS = {
    "ai",
    "bnb",
    "btc",
    "cpi",
    "doj",
    "eth",
    "fbi",
    "fed",
    "gdp",
    "mlb",
    "nba",
    "nfl",
    "nhl",
    "nfp",
    "pce",
    "sec",
    "sol",
    "ufc",
    "wti",
    "xrp",
}


MARKET_INDEX_BODY: dict[str, Any] = {
    "mappings": {
        "properties": {
            "market_pk": {"type": "long"},
            "market_id": {"type": "keyword"},
            "event_id": {"type": "keyword"},
            "ticker": {"type": "keyword"},
            "title": {"type": "search_as_you_type"},
            "subtitle": {"type": "search_as_you_type"},
            "category": {"type": "keyword"},
            "subcategory": {"type": "keyword"},
            "status": {"type": "keyword"},
            "manipulability_prior": {"type": "keyword"},
            "classifier_confidence": {"type": "keyword"},
            "classifier_tags": {"type": "keyword"},
            "keywords": {"type": "keyword"},
            "aliases": {"type": "keyword"},
            "trade_count": {"type": "long"},
            "anomaly_count": {"type": "long"},
            "urgency_score": {"type": "float"},
            "updated_at": {"type": "date"},
            "close_time": {"type": "date"},
        }
    }
}

NEWS_INDEX_BODY: dict[str, Any] = {
    "mappings": {
        "properties": {
            "article_id": {"type": "long"},
            "title": {"type": "search_as_you_type"},
            "summary": {"type": "text"},
            "canonical_url": {"type": "keyword"},
            "domain": {"type": "keyword"},
            "source_tier": {"type": "keyword"},
            "language": {"type": "keyword"},
            "keywords": {"type": "keyword"},
            "entities": {"type": "keyword"},
            "published_at": {"type": "date"},
            "first_seen_at": {"type": "date"},
        }
    }
}


def _opensearch_url(path: str = "") -> str:
    base = settings.search_url.rstrip("/")
    suffix = path if path.startswith("/") or not path else f"/{path}"
    return f"{base}{suffix}"


def market_index_name() -> str:
    return f"{settings.search_index_prefix}-{MARKET_INDEX_SUFFIX}"


def news_index_name() -> str:
    return f"{settings.search_index_prefix}-{NEWS_INDEX_SUFFIX}"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _as_list(value: Iterable[Any] | None) -> list[str]:
    if not value:
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _clean_query(q: str) -> str:
    return " ".join((q or "").strip().split())


def _query_terms(q: str) -> list[str]:
    terms: list[str] = []
    for term in _TOKEN_RE.findall(q.lower()):
        if term in _GENERIC_SEARCH_TERMS:
            continue
        if len(term) < 2:
            continue
        terms.append(term)
    return list(dict.fromkeys(terms))


def _tokens_for_result(*values: object) -> set[str]:
    return set(_TOKEN_RE.findall(" ".join(str(v or "") for v in values).lower()))


def _news_result_matches_query(result: dict, query: str) -> bool:
    terms = _query_terms(query)
    if not terms:
        return True
    tokens = _tokens_for_result(
        result.get("title"),
        result.get("summary"),
        result.get("source"),
        result.get("url"),
    )
    for market in result.get("linked_markets") or []:
        if isinstance(market, dict):
            tokens.update(
                _tokens_for_result(
                    market.get("market_id"),
                    market.get("event_id"),
                    market.get("title"),
                    market.get("subtitle"),
                    market.get("category"),
                )
            )
    hits = [term for term in terms if term in tokens]
    if len(terms) == 1:
        return bool(hits)
    if len(hits) >= min(2, len(terms)):
        return True
    return any(term in _ANCHOR_SEARCH_TERMS for term in hits)


def _filter_news_results_for_query(news: list[dict], query: str) -> list[dict]:
    return [result for result in news if _news_result_matches_query(result, query)]


def _bounded_limit(limit: int) -> int:
    return max(1, min(int(limit or 10), MAX_SEARCH_LIMIT))


def _searchable_market_filters() -> list:
    return [
        Market.status != "unknown",
        Market.title != Market.market_id,
    ]


def _market_metric_counts(metric: MarketMetric | None) -> tuple[int, int]:
    if metric is None:
        return 0, 0
    return int(metric.trade_count or 0), int(metric.anomaly_count or 0)


def market_document(
    market: Market,
    metric: MarketMetric | None = None,
) -> dict[str, Any]:
    profile = profile_for_market(market)
    trade_count, anomaly_count = _market_metric_counts(metric)
    prk = prior_rank(market.manipulability_prior)
    return {
        "market_pk": market.id,
        "market_id": market.market_id,
        "event_id": market.event_id,
        "ticker": market.ticker,
        "title": market.title,
        "subtitle": market.subtitle,
        "category": market.category,
        "subcategory": market.subcategory,
        "status": market.status,
        "manipulability_prior": market.manipulability_prior,
        "classifier_confidence": market.classifier_confidence,
        "classifier_tags": _as_list(market.classifier_tags),
        "keywords": _as_list(profile.get("normalized_keywords")),
        "aliases": _as_list(profile.get("aliases")),
        "trade_count": trade_count,
        "anomaly_count": anomaly_count,
        "market_priority": market_priority_value(market.manipulability_prior),
        "evidence_score": evidence_score_0_100(anomaly_count=anomaly_count),
        "urgency_score": urgency_score_0_100(
            prior_rank=prk,
            anomaly_count=anomaly_count,
        ),
        "open_time": _iso(market.open_time),
        "close_time": _iso(market.close_time),
        "updated_at": _iso(market.updated_at),
    }


def news_document(article: NewsArticle) -> dict[str, Any]:
    return {
        "article_id": article.id,
        "title": article.title,
        "summary": article.summary,
        "canonical_url": article.canonical_url,
        "domain": article.domain,
        "source_tier": article.source_tier,
        "language": article.language,
        "keywords": _as_list(article.keywords),
        "entities": _as_list(article.entities),
        "published_at": _iso(article.published_at),
        "first_seen_at": _iso(article.first_seen_at),
    }


def _market_result_from_doc(doc: dict[str, Any], *, score: float | None) -> dict:
    trade_count = int(doc.get("trade_count") or 0)
    anomaly_count = int(doc.get("anomaly_count") or 0)
    return {
        "kind": "market",
        "market_pk": doc.get("market_pk"),
        "market_id": doc.get("market_id"),
        "event_id": doc.get("event_id"),
        "title": doc.get("title"),
        "subtitle": doc.get("subtitle"),
        "status": doc.get("status"),
        "category": doc.get("category"),
        "subcategory": doc.get("subcategory"),
        "manipulability_prior": doc.get("manipulability_prior"),
        "classifier_confidence": doc.get("classifier_confidence"),
        "trade_count": trade_count,
        "anomaly_count": anomaly_count,
        "market_priority": doc.get("market_priority")
        or market_priority_value(doc.get("manipulability_prior")),
        "evidence_score": doc.get("evidence_score")
        if doc.get("evidence_score") is not None
        else evidence_score_0_100(anomaly_count=anomaly_count),
        "urgency_score": doc.get("urgency_score") or 0,
        "score": score,
        "url": f"/markets/{doc.get('market_id')}",
    }


def _market_result_from_model(
    market: Market,
    metric: MarketMetric | None = None,
    *,
    score: float | None = None,
    reason: str | None = None,
) -> dict:
    doc = market_document(market, metric)
    result = _market_result_from_doc(doc, score=score)
    if reason:
        result["match_reason"] = reason
    return result


def _news_result_from_doc(doc: dict[str, Any], *, score: float | None) -> dict:
    summary = doc.get("summary")
    if isinstance(summary, str) and len(summary) > 280:
        summary = f"{summary[:277].rstrip()}..."
    return {
        "kind": "news",
        "article_id": doc.get("article_id"),
        "title": doc.get("title"),
        "summary": summary,
        "url": doc.get("canonical_url"),
        "source": doc.get("domain"),
        "source_tier": doc.get("source_tier"),
        "language": doc.get("language"),
        "published_at": doc.get("published_at"),
        "first_seen_at": doc.get("first_seen_at"),
        "score": score,
        "linked_market_count": 0,
        "linked_markets": [],
    }


def _news_result_from_model(article: NewsArticle, *, score: float | None = None) -> dict:
    return _news_result_from_doc(news_document(article), score=score)


def _http_client() -> httpx.Client:
    return httpx.Client(timeout=settings.search_timeout_sec)


def opensearch_available() -> bool:
    try:
        with _http_client() as client:
            response = client.get(_opensearch_url())
            return response.status_code < 500
    except httpx.HTTPError:
        return False


def ensure_search_indexes() -> None:
    indexes = {
        market_index_name(): MARKET_INDEX_BODY,
        news_index_name(): NEWS_INDEX_BODY,
    }
    with _http_client() as client:
        for index_name, body in indexes.items():
            response = client.head(_opensearch_url(index_name))
            if response.status_code == 404:
                create = client.put(_opensearch_url(index_name), json=body)
                create.raise_for_status()
            elif response.status_code >= 400:
                response.raise_for_status()


def _bulk_index(index_name: str, docs: list[dict[str, Any]], *, id_field: str) -> int:
    if not docs:
        return 0
    lines: list[str] = []
    for doc in docs:
        lines.append(
            json.dumps(
                {"index": {"_index": index_name, "_id": str(doc[id_field])}},
                separators=(",", ":"),
            )
        )
        lines.append(json.dumps(doc, separators=(",", ":"), default=str))
    payload = "\n".join(lines) + "\n"
    with _http_client() as client:
        response = client.post(
            _opensearch_url("_bulk"),
            content=payload,
            headers={"Content-Type": "application/x-ndjson"},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            failures = [
                item
                for item in body.get("items", [])
                if item.get("index", {}).get("error")
            ][:3]
            raise RuntimeError(f"OpenSearch bulk index had failures: {failures}")
    return len(docs)


def bulk_index_markets(
    db: Session,
    *,
    batch_size: int = 500,
    limit: int | None = None,
) -> int:
    ensure_search_indexes()
    indexed = 0
    offset = 0
    while True:
        remaining = None if limit is None else max(0, limit - indexed)
        if remaining == 0:
            break
        page_size = min(batch_size, remaining) if remaining is not None else batch_size
        rows = (
            db.query(Market, MarketMetric)
            .outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
            .filter(*_searchable_market_filters())
            .order_by(Market.id.asc())
            .offset(offset)
            .limit(page_size)
            .all()
        )
        if not rows:
            break
        docs = [market_document(market, metric) for market, metric in rows]
        indexed += _bulk_index(market_index_name(), docs, id_field="market_pk")
        offset += len(rows)
    return indexed


def bulk_index_news(
    db: Session,
    *,
    batch_size: int = 500,
    limit: int | None = None,
) -> int:
    ensure_search_indexes()
    indexed = 0
    offset = 0
    while True:
        remaining = None if limit is None else max(0, limit - indexed)
        if remaining == 0:
            break
        page_size = min(batch_size, remaining) if remaining is not None else batch_size
        rows = (
            db.query(NewsArticle)
            .order_by(NewsArticle.first_seen_at.desc(), NewsArticle.id.desc())
            .offset(offset)
            .limit(page_size)
            .all()
        )
        if not rows:
            break
        docs = [news_document(article) for article in rows]
        indexed += _bulk_index(news_index_name(), docs, id_field="article_id")
        offset += len(rows)
    return indexed


def _market_opensearch_query(q: str, limit: int) -> dict[str, Any]:
    return {
        "size": limit,
        "query": {
            "bool": {
                "should": [
                    {
                        "multi_match": {
                            "query": q,
                            "type": "bool_prefix",
                            "fields": [
                                "title^4",
                                "title._2gram^3",
                                "title._3gram^2",
                                "subtitle^2",
                                "subtitle._2gram",
                                "subtitle._3gram",
                                "market_id^5",
                                "event_id^3",
                                "ticker^3",
                                "keywords^3",
                                "aliases^3",
                                "classifier_tags^2",
                            ],
                        }
                    },
                    {
                        "multi_match": {
                            "query": q,
                            "fields": [
                                "title^4",
                                "subtitle^2",
                                "market_id^5",
                                "event_id^3",
                                "category",
                                "subcategory",
                                "keywords^2",
                                "aliases^2",
                                "classifier_tags",
                            ],
                            "fuzziness": "AUTO",
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        },
        "sort": [
            {"_score": "desc"},
            {"urgency_score": "desc"},
            {"trade_count": "desc"},
        ],
    }


def _news_opensearch_query(q: str, limit: int) -> dict[str, Any]:
    return {
        "size": limit,
        "query": {
            "bool": {
                "should": [
                    {
                        "multi_match": {
                            "query": q,
                            "type": "bool_prefix",
                            "fields": [
                                "title^5",
                                "title._2gram^3",
                                "title._3gram^2",
                                "summary^2",
                                "domain",
                                "keywords^3",
                                "entities^2",
                            ],
                        }
                    },
                    {
                        "multi_match": {
                            "query": q,
                            "fields": [
                                "title^5",
                                "summary^2",
                                "domain",
                                "keywords^3",
                                "entities^2",
                            ],
                            "fuzziness": "AUTO",
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        },
        "sort": [
            {"_score": "desc"},
            {"first_seen_at": {"order": "desc", "missing": "_last"}},
        ],
    }


def _search_opensearch_index(
    index_name: str,
    body: dict[str, Any],
) -> list[tuple[dict[str, Any], float | None]]:
    with _http_client() as client:
        response = client.post(_opensearch_url(f"{index_name}/_search"), json=body)
        response.raise_for_status()
    hits = response.json().get("hits", {}).get("hits", [])
    return [(hit.get("_source") or {}, hit.get("_score")) for hit in hits]


def search_opensearch(
    q: str,
    *,
    scope: SearchScope,
    limit: int,
) -> tuple[list[dict], list[dict]]:
    market_results: list[dict] = []
    news_results: list[dict] = []
    if scope in ("all", "markets"):
        market_results = [
            _market_result_from_doc(source, score=score)
            for source, score in _search_opensearch_index(
                market_index_name(),
                _market_opensearch_query(q, limit),
            )
        ]
    if scope in ("all", "news"):
        news_results = [
            _news_result_from_doc(source, score=score)
            for source, score in _search_opensearch_index(
                news_index_name(),
                _news_opensearch_query(q, limit),
            )
        ]
    return market_results, news_results


def _direct_market_search(db: Session, q: str, *, limit: int) -> list[dict]:
    like = f"%{q}%"
    rows = (
        db.query(Market, MarketMetric)
        .outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
        .filter(*_searchable_market_filters())
        .filter(
            or_(
                Market.title.ilike(like),
                Market.subtitle.ilike(like),
                Market.market_id.ilike(like),
                Market.event_id.ilike(like),
                Market.ticker.ilike(like),
                Market.category.ilike(like),
                Market.subcategory.ilike(like),
            )
        )
        .order_by(
            MarketMetric.urgency_score.desc().nullslast(),
            MarketMetric.trade_count.desc().nullslast(),
            Market.updated_at.desc(),
        )
        .limit(limit)
        .all()
    )
    return [
        _market_result_from_model(market, metric, score=None, reason="direct_text")
        for market, metric in rows
    ]


def _profile_related_market_search(
    db: Session,
    q: str,
    *,
    limit: int,
    exclude_market_ids: set[str],
) -> list[dict]:
    terms = _query_terms(q)
    if not terms:
        return []

    article = NewsArticle(
        title=q,
        summary=q,
        keywords=terms,
        entities=terms,
        canonical_url_hash="search-query",
        canonical_url="search-query",
        first_seen_at=datetime.now(timezone.utc),
    )
    profile_limit = max(limit * 50, int(settings.search_postgres_profile_limit))
    rows = (
        db.query(MarketNewsProfile, Market, MarketMetric)
        .join(Market, Market.id == MarketNewsProfile.market_pk)
        .outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
        .filter(*_searchable_market_filters())
        .order_by(MarketNewsProfile.updated_at.desc())
        .limit(profile_limit)
        .all()
    )
    profiles = [profile for profile, _market, _metric in rows]
    markets_by_pk = {int(market.id): (market, metric) for profile, market, metric in rows}
    related: list[dict] = []
    for candidate in news_market_candidates(
        article,
        profiles,
        max_candidates=limit * 3,
    ):
        market_and_metric = markets_by_pk.get(int(candidate.profile.market_pk))
        if market_and_metric is None:
            continue
        market, metric = market_and_metric
        if market.market_id in exclude_market_ids:
            continue
        related.append(
            _market_result_from_model(
                market,
                metric,
                score=round(candidate.score, 3),
                reason="profile_terms",
            )
        )
        if len(related) >= limit:
            break
    return related


def _direct_news_search(db: Session, q: str, *, limit: int) -> list[dict]:
    like = f"%{q}%"
    rows = (
        db.query(NewsArticle)
        .filter(
            or_(
                NewsArticle.title.ilike(like),
                NewsArticle.summary.ilike(like),
                NewsArticle.domain.ilike(like),
            )
        )
        .order_by(NewsArticle.first_seen_at.desc(), NewsArticle.id.desc())
        .limit(limit)
        .all()
    )
    return [_news_result_from_model(article) for article in rows]


def _news_from_related_markets(
    db: Session,
    market_results: list[dict],
    *,
    limit: int,
    exclude_article_ids: set[int],
) -> list[dict]:
    market_pks = [
        int(result["market_pk"])
        for result in market_results
        if result.get("market_pk") is not None
    ][:50]
    if not market_pks:
        return []
    rows = (
        db.query(NewsEvent, NewsArticle)
        .join(NewsArticle, NewsArticle.id == NewsEvent.article_id)
        .filter(NewsEvent.market_pk.in_(market_pks))
        .filter(NewsEvent.relevance_score >= 0.35)
        .order_by(
            NewsEvent.pre_news_trade_score.desc(),
            NewsArticle.first_seen_at.desc(),
            NewsEvent.id.desc(),
        )
        .limit(limit * 4)
        .all()
    )
    out: list[dict] = []
    for event, article in rows:
        if article.id in exclude_article_ids:
            continue
        result = _news_result_from_model(article, score=float(event.relevance_score))
        result["pre_news_trade_score"] = float(event.pre_news_trade_score or 0.0)
        exclude_article_ids.add(int(article.id))
        out.append(result)
        if len(out) >= limit:
            break
    return out


def _attach_linked_markets(db: Session, news_results: list[dict]) -> None:
    article_ids = [
        int(result["article_id"])
        for result in news_results
        if result.get("article_id") is not None
    ]
    if not article_ids:
        return
    try:
        counts = {
            int(article_id): int(count)
            for article_id, count in (
                db.query(NewsEvent.article_id, func.count(NewsEvent.id))
                .join(Market, Market.id == NewsEvent.market_pk)
                .filter(NewsEvent.article_id.in_(article_ids))
                .filter(*_searchable_market_filters())
                .group_by(NewsEvent.article_id)
                .all()
            )
        }
        rows = (
            db.query(NewsEvent.article_id, Market, MarketMetric)
            .join(Market, Market.id == NewsEvent.market_pk)
            .outerjoin(MarketMetric, MarketMetric.market_pk == Market.id)
            .filter(NewsEvent.article_id.in_(article_ids))
            .filter(*_searchable_market_filters())
            .order_by(
                NewsEvent.article_id.asc(),
                NewsEvent.relevance_score.desc(),
                NewsEvent.pre_news_trade_score.desc(),
            )
            .all()
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.debug("linked market attachment failed: %s", exc)
        return

    linked: dict[int, list[dict]] = defaultdict(list)
    for article_id, market, metric in rows:
        bucket = linked[int(article_id)]
        if len(bucket) >= 3:
            continue
        bucket.append(_market_result_from_model(market, metric))

    for result in news_results:
        article_id = result.get("article_id")
        if article_id is None:
            continue
        result["linked_market_count"] = counts.get(int(article_id), 0)
        result["linked_markets"] = linked.get(int(article_id), [])


def search_postgres(
    db: Session,
    q: str,
    *,
    scope: SearchScope,
    limit: int,
) -> tuple[list[dict], list[dict]]:
    market_results: list[dict] = []
    news_results: list[dict] = []
    if scope in ("all", "markets"):
        market_results = _direct_market_search(db, q, limit=limit)
        seen = {str(result.get("market_id")) for result in market_results}
        if len(market_results) < limit:
            market_results.extend(
                _profile_related_market_search(
                    db,
                    q,
                    limit=limit - len(market_results),
                    exclude_market_ids=seen,
                )
            )
    if scope in ("all", "news"):
        news_results = _direct_news_search(db, q, limit=limit)
        seen_articles = {
            int(result["article_id"])
            for result in news_results
            if result.get("article_id") is not None
        }
        if len(news_results) < limit:
            seed_markets = market_results
            if not seed_markets:
                seed_markets = _profile_related_market_search(
                    db,
                    q,
                    limit=limit,
                    exclude_market_ids=set(),
                )
            news_results.extend(
                _news_from_related_markets(
                    db,
                    seed_markets,
                    limit=limit - len(news_results),
                    exclude_article_ids=seen_articles,
                )
            )
        _attach_linked_markets(db, news_results)
        news_results = _filter_news_results_for_query(news_results, q)
    return market_results[:limit], news_results[:limit]


def _suggestions(markets: list[dict], news: list[dict], *, limit: int = 8) -> list[dict]:
    suggestions: list[dict] = []
    for market in markets[:4]:
        suggestions.append(
            {
                "kind": "market",
                "label": market.get("title") or market.get("market_id"),
                "value": market.get("market_id"),
                "url": market.get("url"),
            }
        )
    for article in news[:4]:
        suggestions.append(
            {
                "kind": "news",
                "label": article.get("title"),
                "value": article.get("title"),
                "url": article.get("url"),
            }
        )
    return suggestions[:limit]


def dashboard_search(
    db: Session,
    q: str,
    *,
    scope: str = "all",
    limit: int = 10,
) -> dict:
    query = _clean_query(q)
    if scope not in {"all", "markets", "news"}:
        scope = "all"
    limit = _bounded_limit(limit)
    started = time.perf_counter()
    provider = "postgres"
    error: str | None = None

    if len(query) < MIN_QUERY_CHARS:
        return {
            "query": query,
            "scope": scope,
            "provider": "empty",
            "took_ms": 0,
            "markets": [],
            "news": [],
            "suggestions": [],
        }

    try:
        markets, news = search_opensearch(
            query,
            scope=scope,  # type: ignore[arg-type]
            limit=limit,
        )
        _attach_linked_markets(db, news)
        news = _filter_news_results_for_query(news, query)
        provider = "opensearch"
    except Exception as exc:
        db.rollback()
        logger.debug("opensearch search unavailable, using postgres: %s", exc)
        error = exc.__class__.__name__
        markets, news = search_postgres(
            db,
            query,
            scope=scope,  # type: ignore[arg-type]
            limit=limit,
        )

    return {
        "query": query,
        "scope": scope,
        "provider": provider,
        "fallback_reason": error,
        "took_ms": round((time.perf_counter() - started) * 1000, 2),
        "markets": markets,
        "news": news,
        "suggestions": _suggestions(markets, news),
    }
