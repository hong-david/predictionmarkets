"""Contextual suspicious-trade scoring without a trained model.

The older `trade_suspicion` score answers: "is this print unusual for this
market's recent tape?"  This layer asks the more useful surveillance question:
"was the print well-timed, liquidity-sensitive, and market-moving for this
kind of market?"
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any


@dataclass(frozen=True)
class MarketContext:
    market_pk: int
    market_id: str
    category: str | None = None
    subcategory: str | None = None
    manipulability_prior: str | None = None
    event_id: str | None = None
    close_time: datetime | None = None


@dataclass(frozen=True)
class PeerBaseline:
    count_p95: float
    count_p99: float
    abs_price_delta_p95: float
    sample_size: int


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


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(s)
    except ValueError:
        return None
    if out.tzinfo is None:
        return out.replace(tzinfo=timezone.utc)
    return out


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _percentile(vals: list[float], pct: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * pct
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _market_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("category") or "unclassified"),
        str(row.get("subcategory") or "*"),
    )


def _baseline_keys(row: dict[str, Any]) -> list[tuple[str, str]]:
    exact = _market_key(row)
    category_wide = (exact[0], "*")
    if exact == category_wide:
        return [exact]
    return [exact, category_wide]


def build_peer_baselines(
    rows: list[dict[str, Any]],
    *,
    min_points: int = 12,
) -> dict[tuple[str, str], PeerBaseline]:
    """Build category/subcategory peer baselines from recent trade rows.

    Rows should include `category`, `subcategory`, `market_pk`, `yes_price`,
    and `count`. The function is intentionally simple and explainable:
    percentile baselines, grouped by sector, no trained model.
    """
    counts: dict[tuple[str, str], list[float]] = {}
    deltas: dict[tuple[str, str], list[float]] = {}
    last_price_by_market: dict[tuple[str, str, int], float] = {}

    for row in rows:
        count = _f(row.get("count"))
        market_pk = row.get("market_pk")
        yp = _f(row.get("yes_price"))

        for key in _baseline_keys(row):
            if count is not None and count > 0:
                counts.setdefault(key, []).append(count)

            if market_pk is None or yp is None:
                continue
            price_key = (key[0], key[1], int(market_pk))
            prev = last_price_by_market.get(price_key)
            if prev is not None:
                deltas.setdefault(key, []).append(abs(yp - prev))
            last_price_by_market[price_key] = yp

    out: dict[tuple[str, str], PeerBaseline] = {}
    for key, vals in counts.items():
        if len(vals) < min_points:
            continue
        out[key] = PeerBaseline(
            count_p95=_percentile(vals, 0.95),
            count_p99=_percentile(vals, 0.99),
            abs_price_delta_p95=_percentile(deltas.get(key, []), 0.95),
            sample_size=len(vals),
        )
    return out


def _quote_ref(row: dict[str, Any]) -> float | None:
    last = _f(row.get("last_price"))
    if last is not None and last > 0:
        return last
    bid = _f(row.get("yes_bid"))
    ask = _f(row.get("yes_ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return None


def _quote_spread(row: dict[str, Any]) -> float | None:
    bid = _f(row.get("yes_bid"))
    ask = _f(row.get("yes_ask"))
    if bid is None or ask is None:
        return None
    return max(0.0, ask - bid)


def _quote_at_or_before(
    snapshots: list[dict[str, Any]],
    ts: datetime,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for row in snapshots:
        rts = _parse_ts(row.get("ts"))
        if rts is None:
            continue
        if rts <= ts:
            best = row
        else:
            break
    return best


def _first_trade_price_at_or_after(
    trades: list[dict[str, Any]],
    start_idx: int,
    target: datetime,
) -> float | None:
    fallback: float | None = None
    for row in trades[start_idx + 1 :]:
        ts = _parse_ts(row.get("ts"))
        price = _f(row.get("yes_price"))
        if ts is None or price is None:
            continue
        if ts >= target:
            return price
        fallback = price
    return fallback


def _direction(side: str | None) -> int:
    s = (side or "").lower()
    if s == "yes":
        return 1
    if s == "no":
        return -1
    return 0


def _linked_news_after_trade(
    news_events: list[dict[str, Any]],
    trade_ts: datetime,
) -> tuple[dict[str, Any] | None, float | None]:
    best: tuple[dict[str, Any], float] | None = None
    for ev in news_events:
        first_seen = _parse_ts(ev.get("first_seen_at"))
        relevance = _f(ev.get("relevance_score")) or 0.0
        if first_seen is None or first_seen <= trade_ts or relevance < 0.45:
            continue
        hours = (first_seen - trade_ts).total_seconds() / 3600.0
        if hours > 72:
            continue
        if best is None or hours < best[1]:
            best = (ev, hours)
    if best is None:
        return None, None
    return best


def _sibling_abs_moves(
    sibling_snapshots: list[dict[str, Any]],
    trade_ts: datetime,
    *,
    horizon: timedelta = timedelta(minutes=10),
) -> list[float]:
    by_market: dict[int, list[dict[str, Any]]] = {}
    for row in sibling_snapshots:
        mpk = row.get("market_pk")
        if mpk is None:
            continue
        by_market.setdefault(int(mpk), []).append(row)

    moves: list[float] = []
    target_ts = trade_ts + horizon
    for rows in by_market.values():
        before: float | None = None
        after: float | None = None
        for row in rows:
            ts = _parse_ts(row.get("ts"))
            ref = _quote_ref(row)
            if ts is None or ref is None:
                continue
            if ts <= trade_ts:
                before = ref
            elif ts >= target_ts:
                after = ref
                break
            elif after is None:
                after = ref
        if before is not None and after is not None:
            moves.append(abs(after - before))
    return moves


def explain_trades_with_context(
    trades: list[dict[str, Any]],
    *,
    market: MarketContext,
    snapshots: list[dict[str, Any]] | None = None,
    local_explanations: list[dict[str, Any] | None] | None = None,
    peer_baseline: PeerBaseline | None = None,
    news_events: list[dict[str, Any]] | None = None,
    sibling_snapshots: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any] | None]:
    snapshots = snapshots or []
    news_events = news_events or []
    sibling_snapshots = sibling_snapshots or []
    local_explanations = local_explanations or [None] * len(trades)

    out: list[dict[str, Any] | None] = []
    for i, row in enumerate(trades):
        ts = _parse_ts(row.get("ts"))
        price = _f(row.get("yes_price"))
        count = _f(row.get("count"))
        if ts is None or price is None:
            out.append(None)
            continue

        side = str(row.get("taker_side") or "").lower()
        direction = _direction(side)
        prev_price = None
        for prev in reversed(trades[:i]):
            prev_price = _f(prev.get("yes_price"))
            if prev_price is not None:
                break

        q = _quote_at_or_before(snapshots, ts)
        if prev_price is None and q is not None:
            prev_price = _quote_ref(q)
        if prev_price is None:
            prev_price = price

        price_delta = price - prev_price
        abs_delta = abs(price_delta)
        directional_impact = direction * price_delta if direction else 0.0
        post_5m = _first_trade_price_at_or_after(trades, i, ts + timedelta(minutes=5))
        if post_5m is None:
            post_5m = price
        followthrough_5m = direction * (post_5m - prev_price) if direction else 0.0

        open_interest = _f(q.get("open_interest")) if q else None
        volume_24h = _f(q.get("volume_24h")) if q else None
        spread = _quote_spread(q) if q else None
        size_vs_oi = (
            count / open_interest
            if count is not None and open_interest is not None and open_interest > 0
            else None
        )
        size_vs_24h = (
            count / volume_24h
            if count is not None and volume_24h is not None and volume_24h > 0
            else None
        )
        impact_cents_per_100 = (
            (abs_delta * 100.0) * 100.0 / count
            if count is not None and count > 0
            else None
        )

        local_score = (
            float(local_explanations[i]["score"])
            if i < len(local_explanations) and local_explanations[i] is not None
            else 0.0
        )
        components = {
            "local_outlier": round(min(2.5, local_score * 0.25), 3),
            "liquidity_impact": 0.0,
            "followthrough": 0.0,
            "peer_baseline": 0.0,
            "news_timing": 0.0,
            "cross_market": 0.0,
            "priority_context": 0.0,
        }
        reasons: list[str] = []

        if size_vs_oi is not None and size_vs_oi >= 0.02:
            components["liquidity_impact"] += min(1.0, size_vs_oi * 20.0)
            reasons.append("large_size_vs_open_interest")
        if size_vs_24h is not None and size_vs_24h >= 0.08:
            components["liquidity_impact"] += min(0.8, size_vs_24h * 4.0)
            reasons.append("large_size_vs_24h_volume")
        if impact_cents_per_100 is not None and impact_cents_per_100 >= 1.5:
            components["liquidity_impact"] += min(0.8, impact_cents_per_100 / 4.0)
            reasons.append("high_impact_per_contract")
        if spread is not None and spread >= 0.08 and abs_delta >= 0.03:
            components["liquidity_impact"] += 0.4
            reasons.append("moved_through_wide_spread")
        components["liquidity_impact"] = round(
            min(2.0, components["liquidity_impact"]), 3
        )

        if directional_impact >= 0.03 and followthrough_5m >= 0.03:
            components["followthrough"] += 1.2
            reasons.append("price_impact_persisted")
        if followthrough_5m >= 0.08 and abs_delta < 0.03:
            components["followthrough"] += 1.4
            reasons.append("small_trade_before_large_move")
        if directional_impact >= 0.05 and followthrough_5m < 0:
            components["followthrough"] -= 0.5
            reasons.append("impact_reverted")
        components["followthrough"] = round(
            max(0.0, min(2.0, components["followthrough"])), 3
        )

        if peer_baseline is not None and count is not None:
            if count >= peer_baseline.count_p99 and peer_baseline.count_p99 > 0:
                components["peer_baseline"] += 1.2
                reasons.append("large_size_vs_sector_p99")
            elif count >= peer_baseline.count_p95 and peer_baseline.count_p95 > 0:
                components["peer_baseline"] += 0.7
                reasons.append("large_size_vs_sector_p95")
            if (
                peer_baseline.abs_price_delta_p95 > 0
                and abs_delta >= peer_baseline.abs_price_delta_p95
            ):
                components["peer_baseline"] += 0.8
                reasons.append("large_move_vs_sector_peer")
            components["peer_baseline"] = round(
                min(2.0, components["peer_baseline"]), 3
            )

        linked_news, hours_to_news = _linked_news_after_trade(news_events, ts)
        if (
            linked_news is not None
            and hours_to_news is not None
            and followthrough_5m >= 0.03
        ):
            if hours_to_news <= 6:
                components["news_timing"] = 2.0
            elif hours_to_news <= 24:
                components["news_timing"] = 1.2
            else:
                components["news_timing"] = 0.6
            reasons.append("pre_news_directional_move")

        sibling_moves = _sibling_abs_moves(sibling_snapshots, ts)
        sibling_median = median(sibling_moves) if sibling_moves else None
        if sibling_median is not None:
            if sibling_median >= 0.03 and followthrough_5m >= 0.03:
                components["cross_market"] += 1.0
                reasons.append("coherent_event_move")
            elif (
                len(sibling_moves) >= 2
                and abs(followthrough_5m) >= 0.08
                and sibling_median < 0.01
            ):
                components["cross_market"] += 0.7
                reasons.append("isolated_market_move")
            components["cross_market"] = round(min(1.2, components["cross_market"]), 3)

        if (
            market.manipulability_prior in {"high", "medium_high"}
            and sum(v for k, v in components.items() if k != "priority_context") >= 2.0
        ):
            components["priority_context"] = 0.5
            reasons.append("high_priority_market_type")
        close_time = _aware(market.close_time) if market.close_time else None
        if close_time and ts <= close_time <= ts + timedelta(days=1):
            components["priority_context"] = max(components["priority_context"], 0.4)
            reasons.append("near_resolution")

        score = round(min(10.0, sum(components.values())), 3)
        out.append(
            {
                "score": score,
                "reasons": sorted(set(reasons)),
                "components": {k: round(v, 3) for k, v in components.items()},
                "features": {
                    "price_delta": round(price_delta, 4),
                    "directional_impact": round(directional_impact, 4),
                    "followthrough_5m": round(followthrough_5m, 4),
                    "size_vs_open_interest": round(size_vs_oi, 4)
                    if size_vs_oi is not None
                    else None,
                    "size_vs_24h_volume": round(size_vs_24h, 4)
                    if size_vs_24h is not None
                    else None,
                    "spread_at_trade": round(spread, 4) if spread is not None else None,
                    "impact_cents_per_100_contracts": round(impact_cents_per_100, 4)
                    if impact_cents_per_100 is not None
                    else None,
                    "peer_sample_size": peer_baseline.sample_size
                    if peer_baseline
                    else 0,
                    "hours_to_linked_news": round(hours_to_news, 3)
                    if hours_to_news is not None
                    else None,
                    "sibling_abs_move_median_10m": round(sibling_median, 4)
                    if sibling_median is not None
                    else None,
                },
            }
        )
    return out
