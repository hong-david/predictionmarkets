"""Pruning strategy for raw and evidence tables.

This module documents the guardrails used by the retention executors now that
``market_price_history`` and durable evidence tables are the long-term serving
artifacts.
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


RAW_PRUNING_ENABLED = True


RAW_PRUNING_POLICIES: tuple[TablePruningPolicy, ...] = (
    TablePruningPolicy(
        table="market_snapshots",
        enabled=True,
        action=(
            "Prune raw snapshots after compact quote/history coverage exists. "
            "Runtime APIs and scoring should read market_metrics and "
            "market_price_history, not this table."
        ),
        minimum_age_days=7,
        prerequisites=(
            "market_price_history has replacement quote/price buckets",
            "materialized anomaly/case rows carry denormalized quote context",
            "legacy latest_snapshot_id references are no longer required",
        ),
        preserve=(
            "short rollback window during deploys",
            "manually promoted evidence rows until denormalized",
        ),
        rationale=(
            "market_snapshots grew at millions of rows per day. Compact history "
            "and current metrics are the durable serving surfaces."
        ),
    ),
    TablePruningPolicy(
        table="trades",
        enabled=True,
        action=(
            "Keep raw trades through active life plus the tier review window; "
            "then delete closed-market low-value tape only when chart-history "
            "coverage exists and trade evidence has been materialized."
        ),
        minimum_age_days=30,
        prerequisites=(
            "market_price_history contains price buckets for the market",
            "trade_evidence has been materialized for flagged prints",
            "market is closed/resolved beyond the review window",
        ),
        preserve=(
            "trades referenced by trade_flags unless evidence-backed deletion is explicitly enabled",
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
        table="market_price_history",
        enabled=True,
        action=(
            "Keep recent active-market 5-minute buckets, then compact older "
            "buckets to hourly/daily tiers with source rows replaced."
        ),
        minimum_age_days=14,
        prerequisites=(
            "hourly/daily target buckets are upserted before deleting source rows",
            "recent active-market 5-minute window is preserved",
            "operator reviewed dry-run source/target counts",
        ),
        preserve=(
            "recent active 5-minute chart window",
            "high-signal evidence windows when configured",
            "trade and news evidence rows",
        ),
        rationale=(
            "market_price_history replaces snapshots, but it must stay a bounded "
            "read model rather than becoming the next unbounded raw table."
        ),
    ),
    TablePruningPolicy(
        table="anomalies",
        enabled=True,
        action=(
            "Summarize low/none anomalies into daily summaries before deleting "
            "raw rows; materialize qualifying rows into anomaly_evidence before "
            "any raw anomaly deletion."
        ),
        minimum_age_days=7,
        prerequisites=(
            "anomaly_daily_summaries row exists for deleted raw anomalies",
            "anomaly_evidence row exists for evidence-threshold candidates",
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
