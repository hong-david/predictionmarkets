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
    assert result.components["scorer"] == "hybrid_news_relevance_v2"


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


def test_macro_oil_article_gets_direction_hint_from_underlier_terms():
    article = NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/oil",
        title="Oil prices rise as US-Iran peace talks stall",
        keywords=["oil", "prices", "rise"],
    )
    profile = MarketNewsProfile(
        market_pk=3,
        normalized_keywords=["wti", "oil", "above_threshold"],
        entities=[],
        aliases=["KXWTI-TEST"],
        category="macro",
    )

    result = hybrid_news_relevance(article, profile)

    assert result.components["direction_hint"] == "bullish_underlier"


def test_fed_rate_cut_hint_is_bearish_for_rate_underlier():
    article = NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/fed-cut",
        title="Fed signals faster rate cuts after weak inflation data",
        keywords=["fed", "rate", "cuts"],
    )
    profile = MarketNewsProfile(
        market_pk=4,
        normalized_keywords=["fed", "rate", "above_threshold"],
        entities=[],
        aliases=["KXFED-TEST"],
        category="macro",
    )

    result = hybrid_news_relevance(article, profile)

    assert result.components["direction_hint"] == "bearish_underlier"


def test_macro_factor_matching_is_factor_specific():
    article = NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/fed-bank",
        title="Federal Reserve Board announces approval of bank merger application",
        keywords=["federal", "reserve", "approval"],
    )
    oil_profile = MarketNewsProfile(
        market_pk=5,
        normalized_keywords=["wti", "oil", "above_threshold"],
        entities=[],
        aliases=["KXWTI-TEST"],
        category="macro",
    )

    result = hybrid_news_relevance(article, oil_profile)

    assert result.components["factor_relevance"] == 0
    assert result.score < 0.35


def test_corporate_factor_only_article_does_not_link_specific_metric_market():
    article = NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/meta-takeover",
        title="China blocks Meta takeover of AI agent developer Manus",
        summary="Regulators opposed the acquisition.",
    )
    marriott_rooms = MarketNewsProfile(
        market_pk=6,
        normalized_keywords=[
            "marriott",
            "rooms",
            "above_threshold",
            "merger",
            "single_actor_leverage",
        ],
        entities=[],
        aliases=["KXMAR-26MAYROOMS"],
        category="corporate",
    )

    result = hybrid_news_relevance(article, marriott_rooms)

    assert result.components["factor_relevance"] == 0
    assert result.components["factor_hits"] == {}
    assert result.score < 0.35


def test_corporate_metric_article_links_on_specific_underlier_terms():
    article = NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/marriott-rooms",
        title="Marriott reports growth in total rooms ahead of quarterly filing",
        summary="The hotel operator's room count rose again.",
    )
    marriott_rooms = MarketNewsProfile(
        market_pk=7,
        normalized_keywords=["marriott", "rooms", "above_threshold"],
        entities=[],
        aliases=["KXMAR-26MAYROOMS"],
        category="corporate",
    )

    result = hybrid_news_relevance(article, marriott_rooms)

    assert result.components["lexical_relevance"] >= 0.5
    assert result.score >= 0.35
