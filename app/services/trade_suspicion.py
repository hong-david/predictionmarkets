"""Per-trade suspicion scores for UI drill-down (not a legal verdict)."""

from __future__ import annotations

import math
from typing import Any


def _f(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _mean_std(vals: list[float]) -> tuple[float, float]:
    n = len(vals)
    if n < 2:
        return 0.0, 0.0
    mean = sum(vals) / n
    var = sum((a - mean) ** 2 for a in vals) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    return mean, std


def _z(x: float, history: list[float], cap: float = 5.0) -> float:
    if len(history) < 3:
        return 0.0
    m, s = _mean_std(history)
    if s <= 1e-12:
        return 0.0
    return min(cap, max(0.0, abs(x - m) / s))


def score_trades_against_window(
    trades: list[dict[str, Any]],
    *,
    window: int = 50,
) -> list[float | None]:
    """
    `trades` in ascending event time. Returns one non-negative float per
    well-formed row (else ``None``). Uses the previous (up to) ``window`` prints
    for distribution baselines, so the score is a **relative** outlier
    measure only within this market’s tape.
    """
    n = len(trades)
    out: list[float | None] = [None] * n
    if n == 0:
        return out

    size_hist: list[float] = []
    dpx_hist: list[float] = []
    prev_yp: float | None = None

    for i, t in enumerate(trades):
        yp = _f(t.get("yes_price"))
        c = _f(t.get("count"))

        dpx: float | None = None
        if yp is not None and prev_yp is not None:
            dpx = abs(yp - prev_yp)

        s_win = size_hist[-window:]
        d_win = dpx_hist[-window:]

        z_c = _z(c, s_win) if c is not None else 0.0
        z_d = _z(dpx, d_win) if dpx is not None else 0.0

        if c is not None:
            size_hist.append(c)
        if dpx is not None:
            dpx_hist.append(dpx)
        if yp is not None:
            prev_yp = yp

        if c is None and yp is None:
            out[i] = None
            continue

        if z_c == 0.0 and z_d == 0.0:
            out[i] = 0.0
        else:
            out[i] = round(
                min(20.0, 0.5 * (z_c + z_d) + 0.2 * max(z_c, z_d)),
                3,
            )

    return out
