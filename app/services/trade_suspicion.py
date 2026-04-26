"""Per-trade unusual-print scores for UI drill-down.

These scores are a local tape triage tool, not a legal or identity verdict.
They ask whether a public print looks unusual compared with the same market's
recent public prints.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
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


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _robust_z(x: float, history: list[float], cap: float = 6.0) -> float:
    if len(history) < 3:
        return 0.0
    med = _median(history)
    mad = _median([abs(v - med) for v in history])
    if mad > 1e-12:
        return min(cap, max(0.0, abs(x - med) / (1.4826 * mad)))

    _mean, std = _mean_std(history)
    if std <= 1e-12:
        return 0.0
    return min(cap, max(0.0, abs(x - med) / std))


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _side_cluster_score(
    rows: list[tuple[datetime | None, str]],
    *,
    window_sec: float = 30.0,
) -> float:
    if not rows:
        return 0.0
    ts = rows[-1][0]
    if ts is None:
        return 0.0
    side = rows[-1][1]
    if side not in {"yes", "no"}:
        return 0.0

    recent = [
        s
        for t, s in rows
        if t is not None and 0 <= (ts - t).total_seconds() <= window_sec
    ]
    if len(recent) < 3:
        return 0.0
    counts = Counter(recent)
    same = counts.get(side, 0)
    same_frac = same / len(recent)
    if same_frac < 0.65:
        return 0.0
    return min(3.0, (len(recent) - 2) * same_frac / 2.0)


def explain_trades_against_window(
    trades: list[dict[str, Any]],
    *,
    window: int = 50,
) -> list[dict[str, Any] | None]:
    """Return explainable 0..10 unusual-print scores for ascending trades."""
    out: list[dict[str, Any] | None] = [None] * len(trades)
    size_hist: list[float] = []
    dpx_hist: list[float] = []
    recent_side_rows: list[tuple[datetime | None, str]] = []
    prev_yp: float | None = None

    for i, t in enumerate(trades):
        yp = _f(t.get("yes_price"))
        c = _f(t.get("count"))
        ts = _parse_ts(t.get("ts"))
        side = str(t.get("taker_side") or "").lower()

        dpx: float | None = None
        if yp is not None and prev_yp is not None:
            dpx = abs(yp - prev_yp)

        s_win = size_hist[-window:]
        d_win = dpx_hist[-window:]
        log_count = math.log1p(c) if c is not None else None
        z_size = _robust_z(log_count, s_win) if log_count is not None else 0.0
        z_move = _robust_z(dpx, d_win) if dpx is not None else 0.0
        raw_move = min(4.0, (dpx or 0.0) / 0.035)
        cluster = _side_cluster_score(recent_side_rows + [(ts, side)])

        if log_count is not None:
            size_hist.append(log_count)
        if dpx is not None:
            dpx_hist.append(dpx)
        if ts is not None:
            recent_side_rows.append((ts, side))
            recent_side_rows = recent_side_rows[-window:]
        if yp is not None:
            prev_yp = yp

        if c is None and yp is None:
            continue

        cluster_contrib = cluster if z_size >= 1.5 or raw_move >= 0.5 else 0.0
        raw = (
            0.45 * z_size
            + 0.35 * z_move
            + 0.15 * raw_move
            + 0.20 * cluster_contrib
        )
        score = round(min(10.0, (raw / 4.5) * 10.0), 3)
        reasons: list[str] = []
        if z_size >= 2.5:
            reasons.append("large_size_vs_recent")
        if z_move >= 2.5 or raw_move >= 2.0:
            reasons.append("price_jump_vs_recent")
        if cluster_contrib >= 1.5:
            reasons.append("same_side_cluster")

        out[i] = {
            "score": score,
            "reasons": reasons,
            "features": {
                "size_z": round(z_size, 3),
                "move_z": round(z_move, 3),
                "raw_move": round(raw_move, 3),
                "cluster": round(cluster, 3),
                "price_delta": round(dpx, 4) if dpx is not None else None,
            },
        }

    return out


def score_trades_against_window(
    trades: list[dict[str, Any]],
    *,
    window: int = 50,
) -> list[float | None]:
    return [
        None if item is None else float(item["score"])
        for item in explain_trades_against_window(trades, window=window)
    ]
