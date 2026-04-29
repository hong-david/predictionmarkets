from __future__ import annotations

from app.services.trade_suspicion import (
    explain_trades_against_window,
    score_trades_against_window,
)


def test_empty() -> None:
    assert score_trades_against_window([]) == []


def test_flat_tape_stays_low() -> None:
    trades = [
        {"ts": "2020-01-01T00:00:0{i}Z".format(i=i), "yes_price": 0.5, "count": 10.0, "taker_side": "yes"}
        for i in range(15)
    ]
    out = score_trades_against_window(trades, window=50)
    assert len(out) == 15
    for v in out[3:]:
        assert v is not None
        assert v < 0.1


def test_outlier_size_bumps() -> None:
    # Need a non-zero size variance or z-score collapses to 0 (zero std).
    sizes = [3.0, 4.0, 5.0, 6.0, 7.0] * 4  # 20 prints with some spread
    trades: list[dict] = [
        {
            "ts": f"2020-01-01T00:00:{i:02d}Z",
            "yes_price": 0.5,
            "count": sizes[i],
            "taker_side": "yes",
        }
        for i in range(20)
    ]
    trades.append(
        {
            "ts": "2020-01-01T00:00:20Z",
            "yes_price": 0.5,
            "count": 2000.0,
            "taker_side": "yes",
        }
    )
    out = score_trades_against_window(trades, window=50)
    assert (out[-1] or 0) > 1.0


def test_low_dollar_outlier_is_discounted_against_same_size_trade() -> None:
    low_base = [
        {
            "ts": f"2020-01-01T00:00:{i:02d}Z",
            "yes_price": 0.005,
            "no_price": 0.995,
            "count": [3.0, 4.0, 5.0, 6.0, 7.0][i % 5],
            "taker_side": "yes",
        }
        for i in range(20)
    ]
    high_base = [
        {
            "ts": f"2020-01-01T00:00:{i:02d}Z",
            "yes_price": 0.5,
            "no_price": 0.5,
            "count": [3.0, 4.0, 5.0, 6.0, 7.0][i % 5],
            "taker_side": "yes",
        }
        for i in range(20)
    ]
    low_dollars = low_base + [
        {
            "ts": "2020-01-01T00:00:20Z",
            "yes_price": 0.005,
            "no_price": 0.995,
            "count": 2000.0,
            "taker_side": "yes",
        }
    ]
    high_dollars = high_base + [
        {
            "ts": "2020-01-01T00:00:20Z",
            "yes_price": 0.5,
            "no_price": 0.5,
            "count": 2000.0,
            "taker_side": "yes",
        }
    ]

    low = explain_trades_against_window(low_dollars, window=50)[-1]
    high = explain_trades_against_window(high_dollars, window=50)[-1]

    assert low is not None and high is not None
    assert low["features"]["trade_dollar_amount"] == 10.0
    assert "low_notional_discount" in low["reasons"]
    assert high["score"] > low["score"]
    assert low["score"] < high["score"] * 0.25


def test_cluster_of_small_bets_still_surfaces() -> None:
    trades = [
        {
            "ts": f"2020-01-01T00:00:{i:02d}Z",
            "yes_price": 0.5,
            "no_price": 0.5,
            "count": 1.0,
            "taker_side": "no" if i % 2 else "yes",
        }
        for i in range(6)
    ] + [
        {
            "ts": f"2020-01-01T00:00:{i + 6:02d}Z",
            "yes_price": 0.5,
            "no_price": 0.5,
            "count": 1.0,
            "taker_side": "yes",
        }
        for i in range(6)
    ]

    latest = explain_trades_against_window(trades, window=50)[-1]

    assert latest is not None
    assert latest["score"] > 0
    assert latest["score"] < 2
    assert "same_side_cluster" in latest["reasons"]
    assert "small_bet_cluster" in latest["reasons"]
