from __future__ import annotations

import math

from app.services.surveillance_scores import (
    aggregate_reason_codes,
    evidence_score_0_100,
    market_priority_value,
    prior_rank,
    reason_to_code,
    surveillance_urgency_value,
    urgency_score_0_100,
)


def test_prior_rank() -> None:
    assert prior_rank("high") == 4
    assert prior_rank("very_low") == 0
    assert prior_rank(None) == -1


def test_market_priority_value() -> None:
    assert market_priority_value(None) == "unclassified"
    assert market_priority_value("high") == "high"


def test_evidence_score_monotone() -> None:
    assert evidence_score_0_100(anomaly_count=0) == 0
    a1 = evidence_score_0_100(anomaly_count=1)
    a5 = evidence_score_0_100(anomaly_count=5)
    assert 0 < a1 < a5 <= 100


def test_urgency_zero_without_evidence() -> None:
    assert surveillance_urgency_value(4, 0) == 0.0
    assert urgency_score_0_100(prior_rank=4, anomaly_count=0) == 0


def test_urgency_matches_list_formula() -> None:
    u = surveillance_urgency_value(2, 10)
    assert math.isclose(
        u,
        (8.0 + 0.4 * (2 + 1.0)) * math.log(11.0),
    )
    v = urgency_score_0_100(prior_rank=2, anomaly_count=10)
    assert 0 < v <= 100


def test_reason_to_code() -> None:
    assert "wide" in reason_to_code("Wide spread vs recent (z)")


def test_aggregate_reason_codes_dedupes() -> None:
    c = aggregate_reason_codes(["A", "A", "B z", "B z"])
    assert c == [reason_to_code("A"), reason_to_code("b z")]
