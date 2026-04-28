from app.db.models import MarketNewsProfile, NewsArticle
from app.services.news_direction import (
    components_with_market_direction,
    infer_market_orientation,
    orientation_keywords_for_market_text,
    score_market_direction,
)
from app.services.news_relevance import hybrid_news_relevance


def _article(title: str, summary: str | None = None) -> NewsArticle:
    return NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/story",
        title=title,
        summary=summary,
    )


def _profile(*keywords: str, category: str = "crypto_strike") -> MarketNewsProfile:
    return MarketNewsProfile(
        market_pk=1,
        normalized_keywords=list(keywords),
        entities=[],
        aliases=["KXBTC-TEST"],
        category=category,
    )


def test_bullish_crypto_policy_supports_yes_for_above_threshold_market() -> None:
    article = _article(
        "Trump appoints pro digital currency regulator to lead SEC",
        "The appointment is expected to signal a friendlier stance toward digital assets.",
    )
    profile = _profile("will", "bitcoin", "trade", "above", "100000", "year", "end")
    relevance = hybrid_news_relevance(article, profile)

    result = score_market_direction(article, profile, relevance.components)

    assert result.label == "supports_yes"
    assert result.market_orientation == "above_threshold"
    assert result.underlier_direction == "bullish_underlier"
    assert "pro digital currency" in result.evidence_terms
    assert result.confidence >= 0.65


def test_bullish_crypto_policy_supports_no_for_below_threshold_market() -> None:
    article = _article(
        "Trump appoints pro digital currency regulator to lead SEC",
        "The appointment is expected to signal a friendlier stance toward digital assets.",
    )
    profile = _profile("will", "bitcoin", "trade", "below", "80000", "year", "end")
    relevance = hybrid_news_relevance(article, profile)

    result = score_market_direction(article, profile, relevance.components)

    assert result.label == "supports_no"
    assert result.market_orientation == "below_threshold"
    assert result.underlier_direction == "bullish_underlier"


def test_bearish_crypto_article_supports_no_for_above_threshold_market() -> None:
    article = _article(
        "SEC launches crypto crackdown after exchange reserve outflows",
        "Bitcoin fell as regulators opened a new investigation.",
    )
    profile = _profile("will", "bitcoin", "trade", "above", "100000", "year", "end")
    relevance = hybrid_news_relevance(article, profile)

    result = score_market_direction(article, profile, relevance.components)

    assert result.label == "supports_no"
    assert result.market_orientation == "above_threshold"
    assert result.underlier_direction == "bearish_underlier"


def test_relevant_but_directionally_unclear_article_is_ambiguous() -> None:
    article = _article(
        "SEC schedules meeting on digital asset market structure",
        "The agency did not announce rule changes or enforcement actions.",
    )
    profile = _profile("will", "bitcoin", "trade", "above", "100000", "year", "end")
    relevance = hybrid_news_relevance(article, profile)

    result = score_market_direction(article, profile, relevance.components)

    assert relevance.score >= 0.35
    assert result.label == "ambiguous"
    assert result.market_orientation == "above_threshold"


def test_unknown_market_orientation_is_ambiguous() -> None:
    article = _article(
        "Trump appoints pro digital currency regulator to lead SEC",
        "The appointment is expected to signal a friendlier stance toward digital assets.",
    )
    profile = _profile("will", "bitcoin", "end", "year")
    relevance = hybrid_news_relevance(article, profile)

    result = score_market_direction(article, profile, relevance.components)

    assert result.label == "ambiguous"
    assert result.market_orientation == "unknown_orientation"
    assert result.underlier_direction == "bullish_underlier"


def test_non_crypto_approval_word_does_not_create_direction() -> None:
    article = _article("Federal Reserve approves bank merger application")
    profile = _profile(
        "will",
        "oil",
        "trade",
        "above",
        "95",
        category="macro",
    )
    relevance = {
        "lexical_relevance": 0.2,
        "factor_relevance": 0.0,
        "direction_hint": "unknown",
    }

    result = score_market_direction(article, profile, relevance)

    assert result.label == "ambiguous"
    assert result.underlier_direction == "unknown_underlier"


def test_components_with_market_direction_is_json_friendly() -> None:
    article = _article(
        "Trump appoints pro digital currency regulator to lead SEC",
        "The appointment is expected to signal a friendlier stance toward digital assets.",
    )
    profile = _profile("will", "bitcoin", "trade", "above", "100000", "year", "end")
    relevance = hybrid_news_relevance(article, profile)

    components = components_with_market_direction(
        article, profile, relevance.components
    )

    assert components["market_direction"]["label"] == "supports_yes"
    assert isinstance(components["market_direction"]["evidence_terms"], list)
    assert components["market_direction"]["rationale"]


def test_market_orientation_infers_threshold_wording() -> None:
    assert (
        infer_market_orientation(_profile("bitcoin", "greater", "than", "100000"))
        == "above_threshold"
    )
    assert (
        infer_market_orientation(_profile("bitcoin", "less", "than", "80000"))
        == "below_threshold"
    )


def test_orientation_keyword_survives_profile_tokenizer_stopwords() -> None:
    assert orientation_keywords_for_market_text(
        "Will Bitcoin trade above $100,000 by year end?"
    ) == ("above_threshold",)
    assert orientation_keywords_for_market_text(
        "Will Bitcoin trade below $80,000 by year end?"
    ) == ("below_threshold",)


def test_macro_oil_direction_maps_to_market_threshold() -> None:
    article = _article("Oil prices rise as US-Iran peace talks stall")
    profile = _profile("will", "wti", "oil", "trade", "above", "95", category="macro")
    relevance = hybrid_news_relevance(article, profile)

    result = score_market_direction(article, profile, relevance.components)

    assert result.label == "supports_yes"
    assert result.underlier_direction == "bullish_underlier"


def test_rate_cut_direction_supports_no_for_above_rate_market() -> None:
    article = _article("Fed signals faster rate cuts after weak inflation data")
    profile = _profile("will", "fed", "rate", "above", "4.00", category="macro")
    relevance = hybrid_news_relevance(article, profile)

    result = score_market_direction(article, profile, relevance.components)

    assert result.label == "supports_no"
    assert result.underlier_direction == "bearish_underlier"
