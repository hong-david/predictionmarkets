"""Burst / cluster heuristics on the public trade tape (no account ids).

Scores how often many prints land in a short window with a skewed
taker side — a behavioral cluster hypothesis, not identity attribution.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def analyze_tape_bursts(
    trades: list[dict[str, Any]],
    *,
    window_sec: float = 30.0,
) -> dict[str, Any]:
    """
    `trades` ascending by event time.

    Returns a market-level summary and per-print ``cluster_0_10`` for each
    trade (same index order as ``trades``).
    """
    n = len(trades)
    if n == 0:
        return {
            "burst_score_0_10": 0.0,
            "largest_window_count": 0,
            "window_sec": window_sec,
            "dominant_side": None,
            "per_trade_cluster_0_10": [],
        }

    times: list[datetime] = []
    sides: list[str] = []
    for t in trades:
        raw_ts = t.get("ts")
        ts = _parse_ts(str(raw_ts)) if raw_ts is not None else None
        times.append(ts or datetime.min.replace(tzinfo=timezone.utc))
        s = t.get("taker_side")
        sides.append((s or "").lower() if s else "")

    j = 0
    max_c = 0
    for i in range(n):
        if j < i:
            j = i
        while j < n and (times[j] - times[i]).total_seconds() <= window_sec:
            j += 1
        max_c = max(max_c, j - i)

    dominant_at_max: str | None = None
    if max_c >= 2:
        j = 0
        best_imb: float = -1.0
        for i in range(n):
            if j < i:
                j = i
            while j < n and (times[j] - times[i]).total_seconds() <= window_sec:
                j += 1
            c = j - i
            if c != max_c:
                continue
            cnt = Counter(sides[i:j])
            y, no = cnt.get("yes", 0), cnt.get("no", 0)
            if y + no < c * 0.6:
                continue
            top = y if y >= no else no
            imb = top / c
            if imb > best_imb:
                best_imb = imb
                dominant_at_max = "yes" if y >= no else "no"

    if max_c <= 1:
        burst_10 = 0.0
    else:
        burst_10 = min(10.0, 2.0 * math.sqrt(max_c - 1))

    per: list[float] = []
    j2 = 0
    for i2 in range(n):
        if j2 < i2:
            j2 = i2
        while j2 < n and (times[j2] - times[i2]).total_seconds() <= window_sec:
            j2 += 1
        c2 = j2 - i2
        if c2 < 2:
            per.append(0.0)
            continue
        wys = sides[i2:j2]
        cnt2 = Counter(wys)
        top2 = max(cnt2.get("yes", 0), cnt2.get("no", 0))
        same_frac = top2 / c2 if c2 else 0.0
        raw = min(10.0, 1.2 * (c2 - 1) * (0.5 + 0.5 * same_frac))
        per.append(round(raw, 3))

    return {
        "burst_score_0_10": round(burst_10, 3),
        "largest_window_count": int(max_c),
        "window_sec": window_sec,
        "dominant_side": dominant_at_max,
        "per_trade_cluster_0_10": per,
    }
