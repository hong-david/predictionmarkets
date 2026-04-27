"""Lightweight clustering for normalized news articles.

The ingestor reasons over public information events, not raw article counts.
This module clusters near-duplicate headlines/summaries within one ingest run so
syndicated coverage does not inflate evidence by creating many market links for
substantially the same event.

This is intentionally schema-free for v1.  We still upsert every article for
source auditability, but only the cluster representative is linked to markets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import re
from urllib.parse import urlparse

from app.services.news_correlation import NormalizedArticle


_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = {
    "a",
    "about",
    "after",
    "again",
    "against",
    "all",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "new",
    "of",
    "on",
    "or",
    "over",
    "says",
    "that",
    "the",
    "their",
    "this",
    "to",
    "toward",
    "towards",
    "will",
    "with",
}

_TOKEN_ALIASES = {
    # Lightweight headline normalization for near-duplicate clustering. This is
    # intentionally conservative: it only collapses common wording differences
    # that represent the same entity/factor in short news headlines.
    "advocate": "regulator",
    "advocates": "regulator",
    "appointed": "appoint",
    "appoints": "appoint",
    "appointment": "appoint",
    "asset": "asset",
    "assets": "asset",
    "chairman": "chair",
    "currency": "crypto",
    "cryptocurrency": "crypto",
    "cryptocurrencies": "crypto",
    "digital": "crypto",
    "friendlier": "friendly",
    "names": "appoint",
    "nominates": "appoint",
    "nominee": "appoint",
    "nomination": "appoint",
    "regulation": "regulator",
    "regulatory": "regulator",
}


def _canonical_token(token: str) -> str:
    normalized = _TOKEN_ALIASES.get(token, token)
    if len(normalized) > 4 and normalized.endswith("s"):
        singular = normalized[:-1]
        return _TOKEN_ALIASES.get(singular, singular)
    return normalized


@dataclass(frozen=True)
class ArticleEventCluster:
    """A set of articles that appear to describe the same public event."""

    fingerprint: str
    canonical_title: str
    representative: NormalizedArticle
    articles: tuple[NormalizedArticle, ...]
    source_domains: tuple[str, ...]
    first_seen_at: datetime | None
    last_seen_at: datetime | None


@dataclass
class _MutableCluster:
    articles: list[NormalizedArticle]
    token_union: set[str]


def _article_time(article: NormalizedArticle) -> datetime | None:
    return article.first_seen_at or article.published_at


def _domain(url: str) -> str | None:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc or None


def _article_text(article: NormalizedArticle) -> str:
    parts = [
        article.title or "",
        article.summary or "",
        " ".join(article.keywords or ()),
        " ".join(article.entities or ()),
    ]
    return " ".join(parts).lower()


def article_cluster_tokens(article: NormalizedArticle) -> set[str]:
    """Return coarse tokens for event-level near-duplicate detection."""

    tokens = set()
    for raw_token in _TOKEN_RE.findall(_article_text(article)):
        if len(raw_token) <= 2 or raw_token in _STOPWORDS:
            continue

        # Preserve the observed token for explainability and backwards
        # compatibility (for example, "rates" should remain visible), while
        # also adding a canonical token so near-duplicate headlines can cluster
        # across wording differences like "digital assets" vs "crypto".
        tokens.add(raw_token)

        canonical_token = _canonical_token(raw_token)
        if len(canonical_token) <= 2 or canonical_token in _STOPWORDS:
            continue
        tokens.add(canonical_token)
    return tokens


def _token_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if intersection == 0:
        return 0.0
    union = len(left | right)
    jaccard = intersection / union
    containment = intersection / min(len(left), len(right))
    # Jaccard catches broad similarity; containment catches short headline vs
    # longer summary variants of the same event. A small absolute-overlap bonus
    # handles semantically equivalent headlines that use different surrounding
    # wording but share several canonical event tokens.
    overlap_bonus = 0.42 if intersection >= 5 else 0.0
    return max(jaccard, 0.75 * containment, overlap_bonus)


def _within_time_window(
    left: NormalizedArticle,
    right: NormalizedArticle,
    *,
    max_time_distance: timedelta,
) -> bool:
    left_time = _article_time(left)
    right_time = _article_time(right)
    if left_time is None or right_time is None:
        return True
    return abs(left_time - right_time) <= max_time_distance


def _fingerprint_for_articles(articles: tuple[NormalizedArticle, ...]) -> str:
    # URLs differ across syndicated coverage, so fingerprint the stable event
    # vocabulary rather than a specific URL.
    tokens = sorted(set().union(*(article_cluster_tokens(a) for a in articles)))[:32]
    raw = " ".join(tokens)
    if not raw:
        raw = "|".join(sorted(a.canonical_url for a in articles))
    return sha256(raw.encode("utf-8")).hexdigest()


def _representative(articles: list[NormalizedArticle]) -> NormalizedArticle:
    """Pick the clearest article to link as evidence for the cluster."""

    def key(article: NormalizedArticle) -> tuple[int, int, str]:
        title_len = len(article.title or "")
        summary_len = len(article.summary or "")
        # Prefer articles with summaries, then informative titles, with URL as a
        # deterministic tie-breaker.
        return (
            1 if article.summary else 0,
            min(title_len + summary_len, 500),
            article.canonical_url,
        )

    return max(articles, key=key)


def _to_event_cluster(cluster: _MutableCluster) -> ArticleEventCluster:
    articles = tuple(cluster.articles)
    representative = _representative(cluster.articles)
    times = [t for t in (_article_time(article) for article in articles) if t is not None]
    domains = sorted({d for d in (_domain(article.canonical_url) for article in articles) if d})
    return ArticleEventCluster(
        fingerprint=_fingerprint_for_articles(articles),
        canonical_title=representative.title,
        representative=representative,
        articles=articles,
        source_domains=tuple(domains),
        first_seen_at=min(times) if times else None,
        last_seen_at=max(times) if times else None,
    )


def cluster_normalized_articles(
    articles: list[NormalizedArticle],
    *,
    similarity_threshold: float = 0.42,
    max_time_distance: timedelta = timedelta(hours=24),
) -> list[ArticleEventCluster]:
    """Group near-duplicate articles within one ingest batch.

    The clustering is greedy and intentionally lightweight.  It is not a claim
    that articles are semantically identical; it is just a duplicate-control
    layer before market linking.
    """

    ordered = sorted(
        articles,
        key=lambda a: (
            _article_time(a) is None,
            _article_time(a) or datetime.min,
            a.title,
            a.canonical_url,
        ),
    )
    clusters: list[_MutableCluster] = []

    for article in ordered:
        tokens = article_cluster_tokens(article)
        best_cluster: _MutableCluster | None = None
        best_score = 0.0
        for cluster in clusters:
            if not _within_time_window(
                article,
                cluster.articles[0],
                max_time_distance=max_time_distance,
            ):
                continue
            score = _token_similarity(tokens, cluster.token_union)
            if score > best_score:
                best_score = score
                best_cluster = cluster

        if best_cluster is not None and best_score >= similarity_threshold:
            best_cluster.articles.append(article)
            best_cluster.token_union.update(tokens)
        else:
            clusters.append(_MutableCluster(articles=[article], token_union=set(tokens)))

    return [_to_event_cluster(cluster) for cluster in clusters]


def score_components_for_cluster(cluster: ArticleEventCluster) -> dict:
    """Return JSON-friendly metadata to attach to market-link score components."""

    return {
        "event_cluster": {
            "fingerprint": cluster.fingerprint,
            "canonical_title": cluster.canonical_title,
            "article_count": len(cluster.articles),
            "source_count": len(cluster.source_domains),
            "source_domains": list(cluster.source_domains[:10]),
            "first_seen_at": (
                cluster.first_seen_at.isoformat() if cluster.first_seen_at else None
            ),
            "last_seen_at": (
                cluster.last_seen_at.isoformat() if cluster.last_seen_at else None
            ),
            "representative_url": cluster.representative.canonical_url,
            "representative_title": cluster.representative.title,
        }
    }
