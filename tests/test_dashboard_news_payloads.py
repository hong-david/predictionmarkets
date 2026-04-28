from __future__ import annotations

from datetime import datetime, timezone

from app.api.routes.dashboard import (
    _current_news_link_for_market,
    _is_weak_factor_only_news_event,
    _linked_news_article_payload,
    _news_signal_payload,
)
from app.db.models import Market, NewsArticle, NewsEvent


def _article() -> NewsArticle:
    return NewsArticle(
        id=2,
        canonical_url_hash="hash",
        canonical_url="https://example.com/news",
        domain="example.com",
        title="Bitcoin rallies after digital asset policy shift",
        language="English",
        published_at=datetime(2026, 1, 1, 11, 30, tzinfo=timezone.utc),
        first_seen_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )


def _event() -> NewsEvent:
    return NewsEvent(
        id=3,
        article_id=2,
        market_pk=1,
        relevance_score=0.82,
        pre_news_trade_score=7.25,
        leakage_window_seconds=900,
        status="strong_pre_news_signal",
        score_components={
            "lexical_relevance": 0.5,
            "entity_relevance": 0.2,
            "alias_relevance": 0.1,
            "factor_relevance": 0.0,
            "candidate_generation": {
                "candidate_reasons": ["direct_keyword"],
                "matched_terms": ["bitcoin"],
            },
            "market_direction": {"label": "supports_yes", "confidence": 0.7},
            "news_trade_correlation": {
                "reasons": ["direction_aligned_with_news"],
                "best_trade": {"trade_id": "trade-1"},
            },
        },
    )


def test_linked_news_payload_includes_correlation_fields() -> None:
    payload = _linked_news_article_payload(_event(), _article())

    assert payload["title"] == "Bitcoin rallies after digital asset policy shift"
    assert payload["source"] == "example.com"
    assert payload["pre_news_trade_score"] == 7.25
    assert payload["direction_label"] == "supports_yes"
    assert payload["reasons"] == ["direction_aligned_with_news"]
    assert payload["best_trade"] == {"trade_id": "trade-1"}
    assert payload["relevance_components"]["lexical_relevance"] == 0.5
    assert payload["candidate_generation"]["matched_terms"] == ["bitcoin"]


def test_news_signal_payload_includes_market_and_article_summary() -> None:
    market = Market(
        id=1,
        platform="kalshi",
        market_id="KXBTC-100K",
        title="Will Bitcoin trade above 100000?",
        status="open",
        category="crypto_strike",
        manipulability_prior="high",
    )

    payload = _news_signal_payload(_event(), _article(), market)

    assert payload["market_id"] == "KXBTC-100K"
    assert payload["article_source"] == "example.com"
    assert payload["pre_news_trade_score"] == 7.25
    assert payload["direction_label"] == "supports_yes"


def test_weak_factor_only_news_event_is_suppressed() -> None:
    event = _event()
    event.score_components = {
        "lexical_relevance": 0.0,
        "entity_relevance": 0.0,
        "alias_relevance": 0.0,
        "factor_relevance": 0.85,
        "candidate_generation": {
            "candidate_reasons": ["category_factor"],
        },
        "market_direction": {
            "label": "ambiguous",
        },
    }

    assert _is_weak_factor_only_news_event(event)


def test_current_news_link_recheck_suppresses_stale_factor_link() -> None:
    article = NewsArticle(
        id=4,
        canonical_url_hash="fed",
        canonical_url="https://example.com/fed-bank-approval",
        domain="example.com",
        title="Federal Reserve Board announces approval of bank merger application",
        first_seen_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
    )
    market = Market(
        id=5,
        platform="kalshi",
        market_id="KXWTI-TEST",
        title="Will WTI front-month oil be above $96?",
        status="open",
        category="macro",
    )

    assert _current_news_link_for_market(article, market) is None


def test_current_news_link_recheck_keeps_direct_underlier_link() -> None:
    article = _article()
    market = Market(
        id=1,
        platform="kalshi",
        market_id="KXBTC-100K",
        title="Will Bitcoin trade above 100000?",
        status="out_of_scope",
        category="crypto_strike",
        classifier_layer="prefix_rule",
        classifier_confidence="high",
        classifier_tags=["crypto", "btc"],
    )

    current = _current_news_link_for_market(article, market)

    assert current is not None
    relevance, components = current
    assert relevance >= 0.35
    assert components["market_direction"]["label"] == "supports_yes"
