"""Normalized 0–100 scores and reason codes for dashboard JSON.

`evidence_score` is driven by stored `anomalies` row counts (and severity mass).
`urgency_score` matches the *ordering* intent of `surveillance_urgency` in
`app/api/routes/dashboard.py` — evidence dominates; prior nudges.
`market_priority` is the classifier bucket string (a prior, not evidence).
"""

from __future__ import annotations

import math
import re
from typing import Any

# Must stay aligned with `dashboard._PRIOR_RANK` and sort logic.
_PRIOR_RANK: dict[str, int] = {
    "very_low": 0,
    "low": 1,
    "medium": 2,
    "medium_high": 3,
    "high": 4,
}


def prior_rank(manipulability_prior: str | None) -> int:
    if not manipulability_prior:
        return -1
    return _PRIOR_RANK.get(manipulability_prior, -1)


def evidence_score_0_100(*, anomaly_count: int) -> int:
    """0–100 from how much materialized evidence exists (row count, saturating)."""
    ac = max(0, int(anomaly_count))
    if ac == 0:
        return 0
    # ~50 rows maps near the top; long tails still climb slowly.
    return int(min(100, round(100.0 * math.log1p(min(ac, 10_000)) / math.log1p(50.0))))


def surveillance_urgency_value(prior_rank: int, anomaly_count: int) -> float:
    """Same formula as the markets list `surveillance_urgency` sort key."""
    ac = int(anomaly_count)
    if ac <= 0:
        return 0.0
    pr = int(prior_rank)
    if pr < 0:
        pr = 0
    return (8.0 + 0.4 * (pr + 1.0)) * math.log(1.0 + ac)


def urgency_score_0_100(*, prior_rank: int, anomaly_count: int) -> int:
    """0–100 urgency aligned with `surveillance_urgency_value` (capped, rounded)."""
    u = surveillance_urgency_value(prior_rank, anomaly_count)
    if u <= 0:
        return 0
    # Reference scale: U≈50 is "very high" in typical DB; map smoothly to 100.
    return int(min(100, round(100.0 * (1.0 - math.exp(-u / 24.0)))))


def market_priority_value(manipulability_prior: str | None) -> str:
    """Public label for the classifier prior bucket."""
    if manipulability_prior is None or manipulability_prior == "":
        return "unclassified"
    return str(manipulability_prior)


_SLUG = re.compile(r"[^a-z0-9]+")


def reason_to_code(raw: str) -> str:
    """Stable snake_case id for a materialized `reasons[]` string."""
    t = raw.strip().lower()
    t = _SLUG.sub("_", t).strip("_")
    return t[:128] or "unknown"


def aggregate_reason_codes(all_reasons: list[str]) -> list[str]:
    """Deduped machine codes, stable order."""
    seen: set[str] = set()
    out: list[str] = []
    for r in all_reasons:
        c = reason_to_code(r)
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def collect_reason_strings_from_anomaly_json(db_reasons: Any) -> list[str]:
    if not db_reasons:
        return []
    if isinstance(db_reasons, list):
        return [str(x) for x in db_reasons if x is not None]
    return []
