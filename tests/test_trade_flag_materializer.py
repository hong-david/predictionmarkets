from app.services.trade_flag_materializer import (
    final_trade_flag_score,
    severity_for_score,
)


def test_severity_for_score_buckets() -> None:
    assert severity_for_score(3.0) == "low"
    assert severity_for_score(5.0) == "medium"
    assert severity_for_score(7.0) == "high"
    assert severity_for_score(8.5) == "critical"


def test_post_news_discount_applies_to_combined_trade_score() -> None:
    context = {
        "score": 2.0,
        "features": {"post_news_discount_multiplier": 0.65},
    }

    assert final_trade_flag_score(8.0, 2.0, context) == 5.2
