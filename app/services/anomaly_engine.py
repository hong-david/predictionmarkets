"""Compatibility shim for the renamed market-state alert engine.

New code should import from `app.services.market_state_alert_engine`. The
`anomalies` database table and older module path remain for compatibility.
"""

from app.services.market_state_alert_engine import (
    analyze_market,
    compute_mid_price,
    compute_spread,
    compute_volume_delta,
    dec_to_float,
    reference_price,
)

__all__ = [
    "analyze_market",
    "compute_mid_price",
    "compute_spread",
    "compute_volume_delta",
    "dec_to_float",
    "reference_price",
]
