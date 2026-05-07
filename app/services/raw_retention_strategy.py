"""Disabled pruning strategy for raw and evidence tables.

This module is a planning artifact, not an executor. It defines the intended
guards before we delete/summarize more raw production data now that
``market_price_history`` exists as the durable chart source.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TablePruningPolicy:
    table: str
    enabled: bool
    action: str
    minimum_age_days: int | None
    prerequisites: tuple[str, ...]
    preserve: tuple[str, ...]
    rationale: str


RAW_PRUNING_ENABLED = False


RAW_PRUNING_POLICIES: tuple[TablePruningPolicy, ...] = (
    TablePruningPolicy(
        table="market_snapshots",
        enabled=False,
        action=(
            "After chart history is proven, prune closed/resolved-market raw "
            "snapshots only when equivalent market_price_history coverage exists."
        ),
        minimum_age_days=7,
        prerequisites=(
            "market_price_history has 5-minute buckets for the market through close",
            "market has been closed/resolved for at least 7 days",
            "latest_snapshot_id is preserved",
            "snapshots referenced by anomalies are preserved",
        ),
        preserve=(
            "MarketMetric.latest_snapshot_id",
            "Anomaly.latest_snapshot_id",
            "recent active-market raw snapshots",
            "case/triggered market evidence windows",
        ),
        rationale=(
            "Charts should read market_price_history; snapshots become raw "
            "operational evidence and should not be the long-term chart store."
        ),
    ),
    TablePruningPolicy(
        table="trades",
        enabled=False,
        action=(
            "Keep raw trades through active life plus review window; later compact "
            "low-value closed-market tape to aggregate counts/notional while "
            "preserving flagged/case trades."
        ),
        minimum_age_days=30,
        prerequisites=(
            "market_price_history contains price buckets for the market",
            "trade_flags and anomaly references have been materialized",
            "market is closed/resolved beyond the review window",
        ),
        preserve=(
            "trades referenced by trade_flags",
            "trades used in news/trade correlations",
            "case/triggered market evidence windows",
            "high-prior or high-notional review windows",
        ),
        rationale=(
            "Raw trades are evidence, not the chart source. They can be retained "
            "by risk/value once charting no longer depends on every print."
        ),
    ),
    TablePruningPolicy(
        table="anomalies",
        enabled=False,
        action=(
            "Continue summarizing low/none anomalies into daily summaries before "
            "deleting raw rows; keep high/critical and case-linked rows."
        ),
        minimum_age_days=7,
        prerequisites=(
            "anomaly_daily_summaries row exists for deleted raw anomalies",
            "market_metrics anomaly counts are updated",
            "operator has reviewed dry-run counts",
        ),
        preserve=(
            "high/critical anomalies",
            "anomalies with latest_snapshot_id still needed as evidence",
            "case/triggered market windows",
        ),
        rationale=(
            "Anomalies explain suspicious behavior. Low-value noise can compact "
            "to daily summaries, but high-signal evidence should remain raw."
        ),
    ),
    TablePruningPolicy(
        table="trade_flags",
        enabled=False,
        action=(
            "Keep durable high-score trade flags; consider pruning or summarizing "
            "low-score closed-market flags only after UI/audit requirements are clear."
        ),
        minimum_age_days=90,
        prerequisites=(
            "flag score threshold is defined",
            "linked trade preservation is defined",
            "daily/market-level summaries exist if raw rows are removed",
        ),
        preserve=(
            "high-score flags",
            "flags tied to news/trade correlations",
            "flags for case/triggered markets",
        ),
        rationale=(
            "Trade flags are already a compact evidence layer. They should shrink "
            "much more slowly than raw trades."
        ),
    ),
    TablePruningPolicy(
        table="market_metrics",
        enabled=False,
        action="Do not prune while the market row exists; this is the current-state index.",
        minimum_age_days=None,
        prerequisites=("market row deletion/archive policy exists",),
        preserve=("one row per retained market",),
        rationale=(
            "Market metrics powers dashboard/list performance and should remain "
            "small relative to raw event tables."
        ),
    ),
)


def pruning_policy_payload() -> dict:
    return {
        "enabled": RAW_PRUNING_ENABLED,
        "policies": [
            {
                "table": policy.table,
                "enabled": policy.enabled,
                "action": policy.action,
                "minimum_age_days": policy.minimum_age_days,
                "prerequisites": list(policy.prerequisites),
                "preserve": list(policy.preserve),
                "rationale": policy.rationale,
            }
            for policy in RAW_PRUNING_POLICIES
        ],
    }
