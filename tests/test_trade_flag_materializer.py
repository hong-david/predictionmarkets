from sqlalchemy.exc import OperationalError

from app.services.trade_flag_materializer import (
    _is_deadlock_error,
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


def test_deadlock_detection_uses_postgres_sqlstate() -> None:
    class DeadlockOrig(Exception):
        sqlstate = "40P01"

    exc = OperationalError("select 1", {}, DeadlockOrig("deadlock detected"))

    assert _is_deadlock_error(exc)
