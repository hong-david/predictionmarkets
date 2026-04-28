from app.services.historical_signal_qa import _label_case


def test_historical_signal_qa_labels_pre_news_trade_review() -> None:
    label = _label_case(
        {"best_score": 8.5},
        {"best_score": 4.0},
        {"high_count": 0, "count": 0},
    )

    assert label == "strong_pre_news_review"


def test_historical_signal_qa_labels_quiet_market() -> None:
    label = _label_case(
        {"best_score": 0.0},
        {"best_score": 0.0},
        {"high_count": 0, "count": 0},
    )

    assert label == "quiet"
