from app.db.models import MarketNewsProfile, NewsArticle
from app.services.news_candidates import news_market_candidates


def _profile(
    market_pk: int,
    *,
    category: str,
    keywords: list[str],
    aliases: list[str] | None = None,
    entities: list[str] | None = None,
) -> MarketNewsProfile:
    return MarketNewsProfile(
        market_pk=market_pk,
        category=category,
        normalized_keywords=keywords,
        aliases=aliases or [],
        entities=entities or [],
    )


def _article(title: str, summary: str | None = None) -> NewsArticle:
    return NewsArticle(
        canonical_url_hash="x",
        canonical_url="https://example.com/story",
        title=title,
        summary=summary,
    )


def test_crypto_policy_article_candidates_crypto_without_direct_btc_match() -> None:
    article = _article(
        "Trump appoints pro digital currency regulator to lead SEC",
        "The nominee is expected to take a friendlier stance toward digital assets.",
    )
    crypto = _profile(
        1,
        category="crypto_strike",
        keywords=["bitcoin", "btc", "above_threshold"],
    )
    sports = _profile(
        2,
        category="sports_outcome",
        keywords=["yankees", "pitcher"],
    )

    candidates = news_market_candidates(article, [sports, crypto])

    assert [candidate.profile.market_pk for candidate in candidates] == [1]
    assert "category_factor" in candidates[0].reasons


def test_direct_keyword_candidate_survives_without_factor_hit() -> None:
    article = _article("Bitcoin rallies as traders buy crypto exposure")
    crypto = _profile(
        1,
        category="crypto_strike",
        keywords=["bitcoin", "btc", "above_threshold"],
    )

    candidates = news_market_candidates(article, [crypto])

    assert len(candidates) == 1
    assert "keyword_overlap" in candidates[0].reasons
    assert "bitcoin" in candidates[0].matched_terms


def test_single_generic_factor_term_does_not_create_candidate() -> None:
    article = _article("SEC schedules routine public meeting")
    crypto = _profile(
        1,
        category="crypto_strike",
        keywords=["bitcoin", "btc", "above_threshold"],
    )

    assert news_market_candidates(article, [crypto]) == []


def test_category_factor_requires_profile_anchor_to_protect_misclassified_markets() -> None:
    article = _article("Supreme Court considers geofence warrant case")
    misclassified = _profile(
        1,
        category="judicial",
        keywords=["melbourne", "adelaide", "winner"],
    )
    legal = _profile(
        2,
        category="judicial",
        keywords=["supreme", "court", "ruling"],
    )

    candidates = news_market_candidates(article, [misclassified, legal])

    assert [candidate.profile.market_pk for candidate in candidates] == [2]


def test_candidate_limit_keeps_highest_scoring_profiles() -> None:
    article = _article("Bitcoin rallies as BTC breaks higher")
    strong = _profile(
        1,
        category="crypto_strike",
        keywords=["bitcoin", "btc"],
        aliases=["BTC"],
    )
    weaker = _profile(
        2,
        category="crypto_strike",
        keywords=["bitcoin"],
    )

    candidates = news_market_candidates(article, [weaker, strong], max_candidates=1)

    assert [candidate.profile.market_pk for candidate in candidates] == [1]
