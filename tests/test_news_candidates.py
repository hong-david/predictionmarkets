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


def test_generic_profile_words_do_not_crowd_out_specific_matches() -> None:
    article = _article("Bitcoin pulls back from highs as crypto volume cools")
    generic = _profile(
        1,
        category="sports_outcome",
        keywords=["from", "will", "winner"],
    )
    crypto = _profile(
        2,
        category="crypto_strike",
        keywords=["bitcoin", "btc", "crypto"],
    )

    candidates = news_market_candidates(article, [generic, crypto])

    assert [candidate.profile.market_pk for candidate in candidates] == [2]


def test_non_crypto_underlier_keyword_can_candidate_directly() -> None:
    article = _article("Oil prices rise as supply risks grow")
    oil = _profile(
        1,
        category="macro",
        keywords=["wti", "oil", "above_threshold"],
    )

    candidates = news_market_candidates(article, [oil])

    assert [candidate.profile.market_pk for candidate in candidates] == [1]
    assert "oil" in candidates[0].matched_terms


def test_macro_factor_candidate_uses_factor_specific_profile_anchor() -> None:
    article = _article("Federal Reserve Board announces approval of bank merger")
    oil = _profile(
        1,
        category="macro",
        keywords=["wti", "oil", "above_threshold"],
    )
    fed = _profile(
        2,
        category="macro",
        keywords=["fed", "rate", "above_threshold"],
    )

    candidates = news_market_candidates(article, [oil, fed])

    assert [candidate.profile.market_pk for candidate in candidates] == [2]


def test_corporate_factor_only_article_does_not_candidate_specific_metric_market() -> None:
    article = _article(
        "China blocks Meta takeover of AI agent developer Manus",
        "Regulators opposed the acquisition.",
    )
    marriott_rooms = _profile(
        1,
        category="corporate",
        keywords=[
            "marriott",
            "rooms",
            "above_threshold",
            "merger",
            "single_actor_leverage",
        ],
    )

    assert news_market_candidates(article, [marriott_rooms]) == []


def test_corporate_factor_article_can_candidate_matching_company_market() -> None:
    article = _article(
        "China blocks Meta takeover of AI agent developer Manus",
        "Regulators opposed the acquisition.",
    )
    meta_takeover = _profile(
        1,
        category="corporate",
        keywords=["meta", "manus", "acquire", "above_threshold"],
    )

    candidates = news_market_candidates(article, [meta_takeover])

    assert [candidate.profile.market_pk for candidate in candidates] == [1]
    assert "keyword_overlap" in candidates[0].reasons
    assert {"meta", "manus"}.issubset(candidates[0].matched_terms)
