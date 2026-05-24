from __future__ import annotations

from types import SimpleNamespace

from scripts.run_chart_history_compaction import _parse_compaction_policies
from scripts.run_pipeline import build_processes


def test_build_processes_adds_chart_history_compaction_job(tmp_path) -> None:
    args = SimpleNamespace(
        log_dir=str(tmp_path),
        host="127.0.0.1",
        api_port=8000,
        vite_port=5173,
        with_vite=False,
        skip_api=True,
        skip_poller=True,
        skip_ws=True,
        skip_news=True,
        with_retention_maintenance=False,
        with_anomaly_retention=False,
        with_dashboard_cache_warmer=False,
        with_chart_history_compaction=True,
        chart_history_compaction_execute=True,
        chart_history_compaction_replace_source_rows=True,
        chart_history_compaction_interval_seconds=21_600.0,
        chart_history_compaction_grace_days_after_close=7,
        chart_history_compaction_source_interval_sec=300,
        chart_history_compaction_target_interval_sec=3600,
        chart_history_compaction_max_markets=250,
        chart_history_compaction_offset=0,
        chart_history_compaction_max_source_rows_per_market=0,
        chart_history_compaction_policies="300:3600:7,3600:86400:180",
        chart_history_active_retention_policies="300:3600:14,3600:86400:180",
    )

    processes = build_processes(args)

    assert len(processes) == 1
    process = processes[0]
    assert process.key == "chart_history_compaction"
    assert process.label == "Chart history compaction"
    assert process.command[1:3] == ["-m", "scripts.run_chart_history_compaction"]
    assert "--watch" in process.command
    assert "--execute" in process.command
    assert "--replace-source-rows" in process.command
    assert process.command[process.command.index("--interval-seconds") + 1] == "21600.0"
    assert process.command[process.command.index("--max-markets") + 1] == "250"
    assert process.command[process.command.index("--policies") + 1] == "300:3600:7,3600:86400:180"
    assert (
        process.command[process.command.index("--active-retention-policies") + 1]
        == "300:3600:14,3600:86400:180"
    )


def test_parse_chart_history_compaction_policies() -> None:
    policies = _parse_compaction_policies(
        "300:3600:14,3600:86400:180",
        default=[],
    )

    assert policies == [
        {
            "source_interval_sec": 300,
            "target_interval_sec": 3600,
            "grace_days_after_close": 14,
        },
        {
            "source_interval_sec": 3600,
            "target_interval_sec": 86400,
            "grace_days_after_close": 180,
        },
    ]


def test_build_processes_passes_retention_max_batches(tmp_path) -> None:
    args = SimpleNamespace(
        log_dir=str(tmp_path),
        host="127.0.0.1",
        api_port=8000,
        vite_port=5173,
        with_vite=False,
        skip_api=True,
        skip_poller=True,
        skip_ws=True,
        skip_news=True,
        with_retention_maintenance=True,
        retention_execute=True,
        retention_analyze=False,
        retention_interval_seconds=450.0,
        retention_book_event_days=1,
        retention_observe_snapshot_days=1,
        retention_sampled_snapshot_days=3,
        retention_hot_snapshot_days=14,
        retention_batch_size=10000,
        retention_max_batches=100,
        retention_with_trade_retention=True,
        retention_trade_observe_days_after_close=1,
        retention_trade_sampled_days_after_close=14,
        retention_trade_hot_days_after_close=30,
        retention_trade_triggered_days_after_close=90,
        retention_trade_case_days_after_close=365,
        retention_delete_flagged_trades_with_evidence=False,
        retention_allow_uncovered_trade_delete=False,
        retention_allow_uncovered_snapshot_delete=True,
        with_anomaly_retention=False,
        with_dashboard_cache_warmer=False,
        with_chart_history_compaction=False,
    )

    processes = build_processes(args)

    assert len(processes) == 1
    process = processes[0]
    assert process.key == "retention_maintenance"
    assert process.command[1:3] == ["-m", "scripts.run_retention_maintenance"]
    assert process.command[process.command.index("--batch-size") + 1] == "10000"
    assert process.command[process.command.index("--max-batches") + 1] == "100"
    assert "--with-trade-retention" in process.command
    assert "--allow-uncovered-snapshot-delete" in process.command
    assert process.command[process.command.index("--trade-sampled-days-after-close") + 1] == "14"
