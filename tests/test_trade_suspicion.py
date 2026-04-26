from __future__ import annotations

from app.services.trade_suspicion import score_trades_against_window


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
