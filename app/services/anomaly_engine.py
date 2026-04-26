"""Snapshot + order-book heuristics for materialized `anomalies` rows.

When enough history exists, **rolling** baselines (mean / std over recent
snapshots) replace most fixed dollar thresholds. With short history we
fall back to the old static rules so cold-start markets still get a score.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from app.db.models import Market, MarketSnapshot

from app.services.book_activity_signals import book_signals_for_scoring

# Minimum prior intervals to trust a z-score (else use static thresholds).
_MIN_ROLLING = 5
# Stricter than “~2σ” — the goal is fewer spurious materialized rows, not
# exhaustive null-hypothesis tests.
_Z_ROLL = Decimal("2.8")


def dec_to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def compute_spread(snapshot: MarketSnapshot) -> Decimal | None:
    if snapshot.yes_bid_dollars is None or snapshot.yes_ask_dollars is None:
        return None
    return snapshot.yes_ask_dollars - snapshot.yes_bid_dollars


def compute_mid_price(snapshot: MarketSnapshot) -> Decimal | None:
    if snapshot.yes_bid_dollars is None or snapshot.yes_ask_dollars is None:
        return None
    return (snapshot.yes_bid_dollars + snapshot.yes_ask_dollars) / Decimal("2")


def reference_price(snapshot: MarketSnapshot) -> Decimal | None:
    if snapshot.last_price_dollars is not None and snapshot.last_price_dollars > 0:
        return snapshot.last_price_dollars
    return compute_mid_price(snapshot)


def compute_volume_delta(
    latest: MarketSnapshot,
    previous: MarketSnapshot,
) -> Decimal | None:
    if latest.volume_fp is None or previous.volume_fp is None:
        return None
    return latest.volume_fp - previous.volume_fp


def _mean_std(values: list[Decimal]) -> tuple[Decimal | None, Decimal | None]:
    if len(values) < 2:
        return None, None
    n = Decimal(len(values))
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - Decimal("1"))
    if var <= 0:
        return mean, None
    std = Decimal(str(math.sqrt(float(var))))
    return mean, std


def _z_latest(
    latest: Decimal | None,
    history: list[Decimal],
) -> Decimal | None:
    if latest is None or len(history) < _MIN_ROLLING:
        return None
    m, s = _mean_std(history)
    if m is None or s is None or s == 0:
        return None
    return abs(latest - m) / s


def analyze_market(
    market: Market,
    snapshots: list[MarketSnapshot],
    book_activity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    `snapshots`: newest first (``snapshots[0]`` is latest).
    ``book_activity``: output of :func:`collect_book_activity_signals`, or
    ``None`` if not available.
    """
    if not snapshots:
        return {
            "market_id": market.market_id,
            "title": market.title,
            "score": 0.0,
            "severity": "none",
            "reasons": ["no snapshots available"],
            "signals": {},
        }

    latest = snapshots[0]
    previous = snapshots[1] if len(snapshots) > 1 else None

    score = Decimal("0")
    reasons: list[str] = []
    signals: dict[str, Any] = {}

    # --- latest quote context ------------------------------------------------
    latest_spread = compute_spread(latest)
    latest_mid = compute_mid_price(latest)
    latest_ref_price = reference_price(latest)

    signals["latest_snapshot_id"] = latest.id
    signals["latest_snapshot_ts"] = latest.ts.isoformat() if latest.ts else None
    signals["latest_spread"] = dec_to_float(latest_spread)
    signals["latest_mid_price"] = dec_to_float(latest_mid)
    signals["latest_reference_price"] = dec_to_float(latest_ref_price)
    signals["latest_volume_fp"] = dec_to_float(latest.volume_fp)
    signals["latest_liquidity_dollars"] = dec_to_float(latest.liquidity_dollars)
    signals["rolling_window_snapshots"] = min(30, len(snapshots))

    # Build per-snapshot spread series (newest index 0)
    spread_series: list[Decimal] = []
    for i in range(min(30, len(snapshots))):
        sp = compute_spread(snapshots[i])
        if sp is not None:
            spread_series.append(sp)

    price_deltas: list[Decimal] = []
    vol_deltas: list[Decimal] = []
    for i in range(len(snapshots) - 1):
        a, b = snapshots[i], snapshots[i + 1]
        ra, rb = reference_price(a), reference_price(b)
        if ra is not None and rb is not None:
            price_deltas.append(ra - rb)
        vd = compute_volume_delta(a, b)
        if vd is not None:
            vol_deltas.append(vd)

    # --- Rolling z-scores (preferred when enough history) --------------------
    spread_rolling_hit = False
    if latest_spread is not None and len(spread_series) > _MIN_ROLLING:
        z_sp = _z_latest(spread_series[0], spread_series[1:])
        signals["spread_z_vs_recent"] = dec_to_float(z_sp) if z_sp is not None else None
        if z_sp is not None and z_sp > _Z_ROLL:
            score += Decimal("2.5")
            reasons.append("wide spread vs recent baseline (z)")
            spread_rolling_hit = True
    else:
        signals["spread_z_vs_recent"] = None

    signals["wide_spread_rolling"] = spread_rolling_hit
    if not spread_rolling_hit and latest_spread is not None and latest_spread > Decimal("0.12"):
        score += Decimal("2.0")
        reasons.append("wide spread (static threshold)")
        signals["wide_spread"] = True
    else:
        signals["wide_spread"] = spread_rolling_hit or (
            latest_spread is not None and latest_spread > Decimal("0.12")
        )

    # Zero liquidity / empty book (unchanged — rare structural states)
    if latest.liquidity_dollars is not None and latest.liquidity_dollars <= Decimal("0"):
        score += Decimal("1.5")
        reasons.append("zero liquidity")
        signals["zero_liquidity"] = True
    else:
        signals["zero_liquidity"] = False

    if (
        latest.yes_bid_dollars is not None
        and latest.yes_ask_dollars is not None
        and latest.yes_bid_dollars == Decimal("0")
        and latest.yes_ask_dollars == Decimal("0")
    ):
        score += Decimal("1.0")
        reasons.append("empty visible yes book")
        signals["empty_yes_book"] = True
    else:
        signals["empty_yes_book"] = False

    price_rolling_hit = False
    if price_deltas and len(price_deltas) > _MIN_ROLLING:
        z_pr = _z_latest(price_deltas[0], price_deltas[1:])
        signals["price_change_z_vs_recent"] = dec_to_float(z_pr) if z_pr is not None else None
        if z_pr is not None and z_pr > _Z_ROLL:
            score += Decimal("3.0")
            reasons.append("large ref-price move vs recent (z)")
            price_rolling_hit = True
    else:
        signals["price_change_z_vs_recent"] = None

    vol_rolling_hit = False
    if vol_deltas and len(vol_deltas) > _MIN_ROLLING:
        z_v = _z_latest(vol_deltas[0], vol_deltas[1:])
        signals["volume_delta_z_vs_recent"] = dec_to_float(z_v) if z_v is not None else None
        if z_v is not None and z_v > _Z_ROLL:
            score += Decimal("2.5")
            reasons.append("large volume delta vs recent (z)")
            vol_rolling_hit = True
    else:
        signals["volume_delta_z_vs_recent"] = None

    # Static fallbacks when rolling is not available
    if previous is not None and not price_rolling_hit:
        prev_ref_price = reference_price(previous)
        if latest_ref_price is not None and prev_ref_price is not None:
            price_change = latest_ref_price - prev_ref_price
            abs_price_change = abs(price_change)
            signals["price_change"] = dec_to_float(price_change)
            signals["abs_price_change"] = dec_to_float(abs_price_change)
            if abs_price_change >= Decimal("0.15"):
                score += Decimal("3.0")
                reasons.append("sharp price move (static threshold)")
        else:
            signals["price_change"] = None
            signals["abs_price_change"] = None
    else:
        if previous is not None:
            prev_ref_price = reference_price(previous)
            if latest_ref_price is not None and prev_ref_price is not None:
                signals["price_change"] = dec_to_float(latest_ref_price - prev_ref_price)
                signals["abs_price_change"] = dec_to_float(abs(latest_ref_price - prev_ref_price))
        else:
            signals["price_change"] = None
            signals["abs_price_change"] = None

    if previous is not None:
        signals["previous_snapshot_id"] = previous.id
        signals["previous_reference_price"] = dec_to_float(reference_price(previous))
        signals["previous_volume_fp"] = dec_to_float(previous.volume_fp)
        volume_delta = compute_volume_delta(latest, previous)
        signals["volume_delta"] = dec_to_float(volume_delta)
        if not vol_rolling_hit and volume_delta is not None:
            if volume_delta >= Decimal("50"):
                score += Decimal("2.5")
                reasons.append("large volume jump (static threshold)")
            elif volume_delta >= Decimal("25"):
                score += Decimal("1.0")
                reasons.append("moderate volume jump (static threshold)")
    else:
        reasons.append("limited history")
        signals["previous_snapshot_id"] = None
        signals["previous_reference_price"] = None
        signals["previous_volume_fp"] = None
        signals["volume_delta"] = None

    # Order-book activity (last 3 minutes)
    if book_activity:
        bpts, breasons, bsig = book_signals_for_scoring(book_activity)
        score += Decimal(str(bpts))
        reasons.extend(breasons)
        signals["book_activity"] = bsig

    # Severity bucket
    if score >= Decimal("5.0"):
        severity = "high"
    elif score >= Decimal("2.5"):
        severity = "medium"
    elif score > Decimal("0"):
        severity = "low"
    else:
        severity = "none"

    return {
        "market_id": market.market_id,
        "title": market.title,
        "score": float(score),
        "severity": severity,
        "reasons": reasons,
        "signals": signals,
    }
