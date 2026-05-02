from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.historical_signal_qa import _anomaly_summary, _label_case


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


def test_historical_signal_qa_uses_projected_anomaly_counts() -> None:
    last_ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
    metric = SimpleNamespace(
        anomaly_count=12,
        high_anomaly_count=3,
        last_anomaly_ts=last_ts,
    )

    summary = _anomaly_summary(None, object(), metric)

    assert summary == {
        "count": 12,
        "high_count": 3,
        "last_ts": "2026-05-01T00:00:00+00:00",
    }
