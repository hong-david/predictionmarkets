from datetime import datetime, timedelta, timezone

from app.services import market_price_history as history


def test_quote_history_gate_throttles_within_same_bucket() -> None:
    history._last_quote_write_by_market_bucket.clear()
    t0 = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    assert history.should_write_quote_history(1, t0, now_m=0.0) is True
    assert (
        history.should_write_quote_history(
            1, t0 + timedelta(seconds=30), now_m=30.0
        )
        is False
    )
    assert (
        history.should_write_quote_history(
            1, t0 + timedelta(seconds=90), now_m=90.0
        )
        is True
    )


def test_quote_history_gate_prunes_old_bucket_entries(monkeypatch) -> None:
    history._last_quote_write_by_market_bucket.clear()
    monkeypatch.setattr(history, "_QUOTE_CACHE_PRUNE_INTERVAL_SEC", 1.0)
    monkeypatch.setattr(history, "_QUOTE_CACHE_MAX_AGE_BUCKETS", 1)

    t0 = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=10)

    assert history.should_write_quote_history(1, t0, now_m=0.0) is True
    old_key = (1, history.DEFAULT_CHART_HISTORY_INTERVAL_SEC, history.bucket_start(t0))
    assert old_key in history._last_quote_write_by_market_bucket

    assert history.should_write_quote_history(1, t1, now_m=2.0) is True
    assert old_key not in history._last_quote_write_by_market_bucket
