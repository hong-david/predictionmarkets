from app.db.models import MarketNewsProfile, NewsArticle
from app.services.news_relevance import (
    hybrid_news_relevance,
    lexical_relevance,
)


def _crypto_profile() -> MarketNewsProfile:
    return MarketNewsProfile(
        market_pk=1,
        normalized_keywords=["bitcoin", "btc", "threshold"],
        entities=[],
        aliases=["KXBTC-100K"],
        category="crypto_strike",
    )


def test_direct_lexical_match_still_scores():
    article = NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/direct",
        title="Bitcoin rallies as traders bet BTC can break a new threshold",
        keywords=["bitcoin", "btc"],
    )

    result = hybrid_news_relevance(article, _crypto_profile())

    assert lexical_relevance(article, _crypto_profile()) > 0
    assert result.score > 0
    assert result.components["lexical_relevance"] > 0
    assert result.components["scorer"] == "hybrid_news_relevance_v1"


def test_orthogonal_crypto_policy_article_links_above_default_threshold():
    """A BTC market should see crypto-policy catalysts even without "BTC".

    This is the key behavior we want before adding more sources: broad ingest
    can find an article like "Trump names pro-digital-currency SEC regulator"
    and the linker can candidate-match it to crypto strike markets via factor
    exposure, not direct title overlap.
    """

    article = NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/sec-chair",
        title="Trump appoints pro digital currency regulator to lead SEC",
        summary="The appointment is expected to signal a friendlier stance toward digital assets.",
        keywords=["trump", "digital", "currency", "regulator", "sec"],
    )

    result = hybrid_news_relevance(article, _crypto_profile())

    assert result.score >= 0.35
    assert result.components["factor_relevance"] > 0
    assert "crypto_policy" in result.components["factor_hits"]
    assert result.components["direction_hint"] == "bullish_underlier"


def test_unrelated_article_stays_below_default_threshold():
    article = NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/baseball",
        title="Yankees starting pitcher scratched from tonight's game",
        keywords=["yankees", "pitcher", "scratched"],
    )

    result = hybrid_news_relevance(article, _crypto_profile())

    assert result.score < 0.35
    assert result.components["factor_hits"] == {}


def test_summary_text_can_drive_factor_match():
    article = NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/brief-headline",
        title="White House announces new financial nominee",
        summary="The nominee is viewed as pro-crypto and supportive of digital assets.",
    )

    result = hybrid_news_relevance(article, _crypto_profile())

    assert result.score >= 0.35
    assert "crypto_policy" in result.components["factor_hits"]


def test_factor_match_needs_profile_anchor_when_category_is_broad():
    article = NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/scotus",
        title="Supreme Court considers geofence warrant case",
    )
    misclassified = MarketNewsProfile(
        market_pk=2,
        normalized_keywords=["melbourne", "adelaide", "winner"],
        entities=[],
        aliases=[],
        category="judicial",
    )

    result = hybrid_news_relevance(article, misclassified)

    assert result.components["factor_relevance"] == 0
    assert result.score < 0.35
