from app.services.trade_flag_materializer import severity_for_score


def test_severity_for_score_buckets() -> None:
    assert severity_for_score(3.0) == "low"
    assert severity_for_score(5.0) == "medium"
    assert severity_for_score(7.0) == "high"
    assert severity_for_score(8.5) == "critical"
