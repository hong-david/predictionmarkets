from decimal import Decimal
from typing import Any

from app.db.models import Market, MarketSnapshot


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


def analyze_market(
    market: Market,
    snapshots: list[MarketSnapshot],
) -> dict[str, Any]:
    """
    Expects snapshots in descending time order: newest first.
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

    # Signal 1: wide spread
    if latest_spread is not None and latest_spread > Decimal("0.10"):
        score += Decimal("2.0")
        reasons.append("wide spread")
        signals["wide_spread"] = True
    else:
        signals["wide_spread"] = False

    # Signal 2: very low / zero liquidity
    if latest.liquidity_dollars is not None and latest.liquidity_dollars <= Decimal("0"):
        score += Decimal("1.5")
        reasons.append("zero liquidity")
        signals["zero_liquidity"] = True
    else:
        signals["zero_liquidity"] = False

    # Signal 3: empty visible book
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

    # Need previous snapshot for movement-based signals
    if previous is not None:
        prev_ref_price = reference_price(previous)
        prev_volume = previous.volume_fp
        volume_delta = compute_volume_delta(latest, previous)

        signals["previous_snapshot_id"] = previous.id
        signals["previous_reference_price"] = dec_to_float(prev_ref_price)
        signals["previous_volume_fp"] = dec_to_float(prev_volume)
        signals["volume_delta"] = dec_to_float(volume_delta)

        # Signal 4: sharp price move
        if latest_ref_price is not None and prev_ref_price is not None:
            price_change = latest_ref_price - prev_ref_price
            abs_price_change = abs(price_change)
            signals["price_change"] = dec_to_float(price_change)
            signals["abs_price_change"] = dec_to_float(abs_price_change)

            if abs_price_change >= Decimal("0.15"):
                score += Decimal("3.0")
                reasons.append("sharp price move")
        else:
            signals["price_change"] = None
            signals["abs_price_change"] = None

        # Signal 5: sudden new volume
        if volume_delta is not None:
            if volume_delta >= Decimal("25"):
                score += Decimal("2.5")
                reasons.append("large volume jump")
            elif volume_delta >= Decimal("10"):
                score += Decimal("1.0")
                reasons.append("moderate volume jump")
    else:
        reasons.append("limited history")
        signals["previous_snapshot_id"] = None
        signals["previous_reference_price"] = None
        signals["previous_volume_fp"] = None
        signals["volume_delta"] = None
        signals["price_change"] = None
        signals["abs_price_change"] = None

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