from app.db.models import MarketNewsProfile, NewsArticle
from app.services.news_link_scoring import score_article_market_link


def _article(title: str, summary: str | None = None, *, url: str = "https://example.com/story") -> NewsArticle:
    return NewsArticle(
        canonical_url_hash="x",
        canonical_url=url,
        title=title,
        summary=summary,
    )


def _profile(*keywords: str, category: str) -> MarketNewsProfile:
    return MarketNewsProfile(
        market_pk=1,
        normalized_keywords=list(keywords),
        entities=[],
        aliases=["KXTEST"],
        category=category,
    )


def test_corporate_factor_only_article_is_blocked_for_metric_market() -> None:
    article = _article(
        "China blocks $2bn Meta takeover of AI agent developer Manus",
        "Regulators opposed the acquisition.",
    )
    profile = _profile(
        "marriott",
        "rooms",
        "above_threshold",
        "merger",
        category="corporate",
    )
    components = {
        "candidate_score": 2.5,
        "candidate_reasons": ["category_factor"],
        "matched_terms": ["corporate_event", "acquisition", "takeover"],
    }

    result = score_article_market_link(
        article,
        profile,
        min_relevance=0.35,
        candidate_component=components,
    )

    assert result is None


def test_corporate_direct_underlier_article_is_kept() -> None:
    article = _article(
        "Marriott reports growth in total rooms ahead of quarterly filing",
        "The hotel operator's room count rose again.",
    )
    profile = _profile("marriott", "rooms", "above_threshold", category="corporate")

    result = score_article_market_link(article, profile, min_relevance=0.35)

    assert result is not None
    assert result.components["category_gate"]["allowed"] is True
    assert result.components["category_gate"]["direct_anchor"] is True


def test_crypto_policy_factor_article_can_link_without_direct_btc_word() -> None:
    article = _article(
        "Trump appoints pro digital currency regulator to lead SEC",
        "The appointment is expected to signal a friendlier stance toward digital assets.",
    )
    profile = _profile("bitcoin", "btc", "above_threshold", category="crypto_strike")

    result = score_article_market_link(article, profile, min_relevance=0.35)

    assert result is not None
    assert result.components["category_gate"]["factor_only"] is True


def test_sports_injury_article_needs_team_or_player_anchor() -> None:
    article = _article("Yankees starting pitcher scratched from tonight's game")
    profile = _profile("lakers", "celtics", "winner", category="sports_outcome")

    result = score_article_market_link(article, profile, min_relevance=0.20)

    assert result is None
