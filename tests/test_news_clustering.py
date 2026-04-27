from datetime import datetime, timezone

from app.services.news_clustering import (
    article_cluster_tokens,
    cluster_normalized_articles,
    score_components_for_cluster,
)
from app.services.news_correlation import NormalizedArticle


def _article(url: str, title: str, summary: str | None = None) -> NormalizedArticle:
    ts = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    return NormalizedArticle(
        canonical_url=url,
        title=title,
        summary=summary,
        published_at=ts,
        first_seen_at=ts,
        source_tier="test",
    )


def test_similar_articles_cluster_into_one_event():
    articles = [
        _article(
            "https://example.com/a",
            "Trump appoints pro crypto regulator to lead SEC",
            "The new SEC chair is viewed as friendly to digital assets.",
        ),
        _article(
            "https://wire.example/b",
            "Trump names pro digital asset advocate as SEC chair",
            "The nominee is expected to take a friendlier stance toward crypto regulation.",
        ),
    ]

    clusters = cluster_normalized_articles(articles)

    assert len(clusters) == 1
    assert clusters[0].fingerprint
    assert clusters[0].source_domains == ("example.com", "wire.example")
    assert clusters[0].canonical_title in {article.title for article in articles}


def test_unrelated_articles_remain_separate_events():
    articles = [
        _article(
            "https://example.com/crypto",
            "Trump appoints pro crypto regulator to lead SEC",
        ),
        _article(
            "https://example.com/baseball",
            "Yankees starting pitcher scratched from tonight's game",
        ),
    ]

    clusters = cluster_normalized_articles(articles)

    assert len(clusters) == 2


def test_cluster_components_are_json_friendly():
    cluster = cluster_normalized_articles(
        [
            _article(
                "https://example.com/a",
                "Trump appoints pro crypto regulator to lead SEC",
                "The new SEC chair is viewed as friendly to digital assets.",
            ),
            _article(
                "https://wire.example/b",
                "Trump names pro digital asset advocate as SEC chair",
                (
                    "The nominee is expected to take a friendlier stance "
                    "toward crypto regulation."
                ),
            ),
        ]
    )[0]

    components = score_components_for_cluster(cluster)

    assert components["event_cluster"]["article_count"] == 2
    assert components["event_cluster"]["source_count"] == 2
    assert components["event_cluster"]["representative_url"].startswith("https://")
    assert components["event_cluster"]["first_seen_at"].startswith("2026-04-26")


def test_cluster_tokens_ignore_common_words():
    tokens = article_cluster_tokens(
        _article("https://example.com/x", "The Fed says it will decide on rates")
    )

    assert "the" not in tokens
    assert "will" not in tokens
    assert "fed" in tokens
    assert "rates" in tokens
