from __future__ import annotations

from app.services.trade_burst import analyze_tape_bursts


def _many_same_side(n: int) -> list[dict]:
    return [
        {
            "ts": f"2020-01-01T00:00:{i:02d}Z",
            "count": 1.0,
            "taker_side": "yes",
        }
        for i in range(n)
    ]


def test_empty_tape() -> None:
    b = analyze_tape_bursts([], window_sec=30.0)
    assert b["burst_score_0_10"] == 0.0
    assert b["per_trade_cluster_0_10"] == []


def test_cluster_window_small_tape() -> None:
    trades = _many_same_side(8)
    b = analyze_tape_bursts(trades, window_sec=200.0)
    assert b["largest_window_count"] >= 2
    assert b["burst_score_0_10"] > 0.0
    assert len(b["per_trade_cluster_0_10"]) == 8
    assert max(b["per_trade_cluster_0_10"]) > 0.0
