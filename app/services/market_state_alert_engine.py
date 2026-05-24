"""Market-state alert rules for quote, snapshot, and order-book conditions.

This module powers materialized rows in the legacy-compatible `anomalies`
table. The rows are market-state alerts: wide spreads, abrupt reference-price
moves, volume jumps, zero liquidity, and recent book churn. They are not
per-trade suspiciousness verdicts; durable per-execution scoring lives in
`trade_context` and `trade_flag_materializer`.

When enough history exists, rolling baselines replace most fixed thresholds.
With short history we keep static fallbacks so cold-start markets still get a
stable score.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.db.models import Market
from app.services.book_activity_signals import book_signals_for_scoring
from app.services.features.liquidity import decimal_volume_delta
from app.services.features.quotes import (
    decimal_mid_price,
    decimal_reference_price,
    decimal_spread,
)
from app.services.features.rolling import decimal_latest_z_score

# Minimum prior intervals to trust a z-score (else use static thresholds).
_MIN_ROLLING = 5
# Stricter than about 2 sigma: fewer spurious materialized rows beats exhaustive
# null-hypothesis testing for the dashboard alert trail.
_Z_ROLL = Decimal("2.8")


def dec_to_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def compute_spread(snapshot: Any) -> Decimal | None:
    return decimal_spread(snapshot.yes_bid_dollars, snapshot.yes_ask_dollars)


def compute_mid_price(snapshot: Any) -> Decimal | None:
    return decimal_mid_price(snapshot.yes_bid_dollars, snapshot.yes_ask_dollars)


def reference_price(snapshot: Any) -> Decimal | None:
    return decimal_reference_price(
        last=snapshot.last_price_dollars,
        bid=snapshot.yes_bid_dollars,
        ask=snapshot.yes_ask_dollars,
    )


def compute_volume_delta(
    latest: Any,
    previous: Any,
) -> Decimal | None:
    return decimal_volume_delta(latest.volume_fp, previous.volume_fp)



def _reference_price_mode(snapshot: Any) -> str:
    """Return how reference_price() will be derived for this snapshot."""
    if getattr(snapshot, "last_price_dollars", None) is not None:
        return "last"
    if (
        getattr(snapshot, "yes_bid_dollars", None) is not None
        and getattr(snapshot, "yes_ask_dollars", None) is not None
    ):
        return "mid"
    return "none"


def _volume_semantics(snapshot: Any) -> str:
    """Return a coarse snapshot-volume shape.

    Missing optional fields are treated as the partial shape so lightweight
    test doubles that omit DB-only columns still work.
    """
    if getattr(snapshot, "source", None) == "chart_history":
        return "bucket"

    complete_quote = (
        getattr(snapshot, "last_price_dollars", None) is not None
        and getattr(snapshot, "no_bid_dollars", None) is not None
        and getattr(snapshot, "no_ask_dollars", None) is not None
        and getattr(snapshot, "volume_24h_fp", None) is not None
    )
    return "complete" if complete_quote else "partial"


def _reference_price_comparable(
    current: Any,
    previous: Any,
) -> bool:
    mode = _reference_price_mode(current)
    return mode != "none" and mode == _reference_price_mode(previous)


def _volume_comparable(
    current: Any,
    previous: Any,
) -> bool:
    if current.volume_fp is None or previous.volume_fp is None:
        return False
    return _volume_semantics(current) == _volume_semantics(previous)


def _series_spreads(snapshots: list[Any]) -> list[Decimal]:
    spreads: list[Decimal] = []
    for snapshot in snapshots[: min(30, len(snapshots))]:
        spread = compute_spread(snapshot)
        if spread is not None:
            spreads.append(spread)
    return spreads


def _price_and_volume_deltas(
    snapshots: list[Any],
) -> tuple[list[Decimal], list[Decimal]]:
    price_deltas: list[Decimal] = []
    volume_deltas: list[Decimal] = []
    for i in range(len(snapshots) - 1):
        current, previous = snapshots[i], snapshots[i + 1]
        current_ref = reference_price(current)
        previous_ref = reference_price(previous)
        if (
            _reference_price_comparable(current, previous)
            and current_ref is not None
            and previous_ref is not None
        ):
            price_deltas.append(current_ref - previous_ref)
        if _volume_comparable(current, previous):
            if _volume_semantics(current) == "bucket":
                volume_value = getattr(current, "volume_fp", None)
                if volume_value is not None:
                    volume_deltas.append(volume_value)
            else:
                volume_delta = compute_volume_delta(current, previous)
                if volume_delta is not None:
                    volume_deltas.append(volume_delta)
    return price_deltas, volume_deltas


def analyze_market(
    market: Market,
    snapshots: list[Any],
    book_activity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score market-state alert conditions from newest-first snapshots."""
    if not snapshots:
        return {
            "market_id": market.market_id,
            "title": market.title,
            "score": 0.0,
            "severity": "none",
            "reasons": ["no quote history available"],
            "signals": {},
        }

    latest = snapshots[0]
    previous = snapshots[1] if len(snapshots) > 1 else None

    score = Decimal("0")
    reasons: list[str] = []
    signals: dict[str, Any] = {}

    latest_spread = compute_spread(latest)
    latest_mid = compute_mid_price(latest)
    latest_ref_price = reference_price(latest)

    signals["latest_snapshot_id"] = getattr(latest, "id", None)
    signals["latest_snapshot_ts"] = latest.ts.isoformat() if latest.ts else None
    signals["latest_quote_source"] = getattr(latest, "source", "unknown")
    signals["latest_quote_source_key"] = getattr(latest, "source_key", None)
    signals["latest_quote_ts"] = latest.ts.isoformat() if latest.ts else None
    signals["latest_spread"] = dec_to_float(latest_spread)
    signals["latest_mid_price"] = dec_to_float(latest_mid)
    signals["latest_reference_price"] = dec_to_float(latest_ref_price)
    signals["latest_reference_price_mode"] = _reference_price_mode(latest)
    signals["latest_volume_semantics"] = _volume_semantics(latest)
    signals["latest_volume_fp"] = dec_to_float(latest.volume_fp)
    signals["latest_liquidity_dollars"] = dec_to_float(latest.liquidity_dollars)
    signals["rolling_window_snapshots"] = min(30, len(snapshots))

    spread_series = _series_spreads(snapshots)
    price_deltas, volume_deltas = _price_and_volume_deltas(snapshots)

    spread_rolling_hit = False
    if latest_spread is not None and len(spread_series) > _MIN_ROLLING:
        z_spread = decimal_latest_z_score(
            spread_series[0],
            spread_series[1:],
            min_history=_MIN_ROLLING,
        )
        signals["spread_z_vs_recent"] = dec_to_float(z_spread)
        if z_spread is not None and z_spread > _Z_ROLL:
            score += Decimal("2.5")
            reasons.append("wide spread vs recent baseline (z)")
            spread_rolling_hit = True
    else:
        signals["spread_z_vs_recent"] = None

    signals["wide_spread_rolling"] = spread_rolling_hit
    if (
        not spread_rolling_hit
        and latest_spread is not None
        and latest_spread > Decimal("0.12")
    ):
        score += Decimal("2.0")
        reasons.append("wide spread (static threshold)")
        signals["wide_spread"] = True
    else:
        signals["wide_spread"] = spread_rolling_hit or (
            latest_spread is not None and latest_spread > Decimal("0.12")
        )

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
        z_price = decimal_latest_z_score(
            price_deltas[0],
            price_deltas[1:],
            min_history=_MIN_ROLLING,
        )
        signals["price_change_z_vs_recent"] = dec_to_float(z_price)
        if z_price is not None and z_price > _Z_ROLL:
            score += Decimal("3.0")
            reasons.append("large ref-price move vs recent (z)")
            price_rolling_hit = True
    else:
        signals["price_change_z_vs_recent"] = None

    volume_rolling_hit = False
    if volume_deltas and len(volume_deltas) > _MIN_ROLLING:
        z_volume = decimal_latest_z_score(
            volume_deltas[0],
            volume_deltas[1:],
            min_history=_MIN_ROLLING,
        )
        signals["volume_delta_z_vs_recent"] = dec_to_float(z_volume)
        if z_volume is not None and z_volume > _Z_ROLL:
            score += Decimal("2.5")
            reasons.append("large volume delta vs recent (z)")
            volume_rolling_hit = True
    else:
        signals["volume_delta_z_vs_recent"] = None

    if previous is not None and not price_rolling_hit:
        prev_ref_price = reference_price(previous)
        signals["price_delta_comparable"] = _reference_price_comparable(latest, previous)
        if (
            signals["price_delta_comparable"]
            and latest_ref_price is not None
            and prev_ref_price is not None
        ):
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
                price_change = latest_ref_price - prev_ref_price
                signals["price_change"] = dec_to_float(price_change)
                signals["abs_price_change"] = dec_to_float(abs(price_change))
        else:
            signals["price_change"] = None
            signals["abs_price_change"] = None

    if previous is not None:
        signals["previous_snapshot_id"] = getattr(previous, "id", None)
        signals["previous_quote_source"] = getattr(previous, "source", "unknown")
        signals["previous_quote_source_key"] = getattr(previous, "source_key", None)
        signals["previous_reference_price"] = dec_to_float(reference_price(previous))
        signals["previous_reference_price_mode"] = _reference_price_mode(previous)
        signals["previous_volume_semantics"] = _volume_semantics(previous)
        signals["previous_volume_fp"] = dec_to_float(previous.volume_fp)
        signals["volume_delta_comparable"] = _volume_comparable(latest, previous)
        if signals["volume_delta_comparable"] and _volume_semantics(latest) == "bucket":
            volume_delta = latest.volume_fp
        else:
            volume_delta = (
                compute_volume_delta(latest, previous)
                if signals["volume_delta_comparable"]
                else None
            )
        signals["volume_delta"] = dec_to_float(volume_delta)
        if not volume_rolling_hit and volume_delta is not None:
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

    if book_activity:
        book_points, book_reasons, book_signals = book_signals_for_scoring(book_activity)
        score += Decimal(str(book_points))
        reasons.extend(book_reasons)
        signals["book_activity"] = book_signals

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

