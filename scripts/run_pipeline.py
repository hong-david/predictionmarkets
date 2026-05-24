"""Run the local surveillance pipeline under one lightweight supervisor.

The child processes still own their work and write their own heartbeats. This
script gives local development one stable entry point, pins the API port, keeps
logs in one directory, and restarts failed children with a small backoff.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    new_run_id,
    record_pipeline_heartbeat,
)
from app.services.market_price_history import DEFAULT_CHART_HISTORY_INTERVAL_SEC


CHART_HISTORY_COMPACTION_TARGET_INTERVAL_SEC = 3600


@dataclass
class ManagedProcess:
    key: str
    label: str
    command: list[str]
    cwd: Path
    log_dir: Path
    process: subprocess.Popen | None = None
    restart_count: int = 0
    backoff_seconds: float = 1.0
    forward_output: bool = False
    stdout_handle: object | None = field(default=None, repr=False)
    stderr_handle: object | None = field(default=None, repr=False)

    def start(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stdout = None
        stderr = None
        if not self.forward_output:
            self.stdout_handle = (self.log_dir / f"{self.key}.log").open("ab")
            self.stderr_handle = (self.log_dir / f"{self.key}.err").open("ab")
            stdout = self.stdout_handle
            stderr = self.stderr_handle
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            self.command,
            cwd=self.cwd,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
        self.backoff_seconds = 1.0

    def poll(self) -> int | None:
        if self.process is None:
            return None
        return self.process.poll()

    def restart(self) -> None:
        self.close_handles()
        time.sleep(self.backoff_seconds)
        self.restart_count += 1
        self.backoff_seconds = min(self.backoff_seconds * 2, 30.0)
        self.start()

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.close_handles()

    def close_handles(self) -> None:
        for handle in (self.stdout_handle, self.stderr_handle):
            if handle is not None:
                handle.close()
        self.stdout_handle = None
        self.stderr_handle = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _npm_command() -> str:
    return "npm.cmd" if os.name == "nt" else "npm"


def build_processes(args: argparse.Namespace) -> list[ManagedProcess]:
    root = _repo_root()
    py = sys.executable
    log_dir = Path(args.log_dir)
    if not log_dir.is_absolute():
        log_dir = root / log_dir
    processes: list[ManagedProcess] = []

    if not args.skip_api:
        processes.append(
            ManagedProcess(
                key="api",
                label="API",
                command=[
                    py,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    args.host,
                    "--port",
                    str(args.api_port),
                ],
                cwd=root,
                log_dir=log_dir,
            )
        )
    if args.with_vite:
        processes.append(
            ManagedProcess(
                key="vite",
                label="Vite dev server",
                command=[
                    _npm_command(),
                    "run",
                    "dev",
                    "--",
                    "--host",
                    args.host,
                    "--port",
                    str(args.vite_port),
                ],
                cwd=root / "frontend",
                log_dir=log_dir,
            )
        )
    if not args.skip_poller:
        processes.append(
            ManagedProcess(
                key="market_poller",
                label="Market poller / hydration",
                command=[
                    py,
                    "-m",
                    "scripts.poll_markets",
                    "--interval",
                    str(args.poll_interval_seconds),
                    "--batch",
                    str(args.poll_batch_size),
                    "--anomaly-market-limit",
                    str(args.anomaly_market_limit),
                    "--hydrate-unknown-max",
                    str(args.hydrate_unknown_max),
                    "--error-backoff-seconds",
                    str(args.poll_error_backoff_seconds),
                    "--refresh-active-lifecycle-max",
                    str(args.refresh_active_lifecycle_max),
                    "--refresh-active-lifecycle-min-age-minutes",
                    str(args.refresh_active_lifecycle_min_age_minutes),
                    "--refresh-active-lifecycle-sleep",
                    str(args.refresh_active_lifecycle_sleep),
                ],
                cwd=root,
                log_dir=log_dir,
            )
        )
    if not args.skip_ws:
        processes.append(
            ManagedProcess(
                key="ws_trade_feed",
                label="WebSocket trade feed",
                command=[py, "-m", "scripts.run_ws_ticker_consumer"],
                cwd=root,
                log_dir=log_dir,
            )
        )
    if not args.skip_news:
        news_command = [
            py,
            "-m",
            "scripts.run_news_surveillance_pipeline",
            "--watch",
            "--interval-seconds",
            str(args.news_interval_seconds),
            "--lookback-hours",
            str(args.news_lookback_hours),
            "--news-limit",
            str(args.news_limit),
            "--max-markets",
            str(args.news_max_markets),
            "--max-profiles",
            str(args.news_max_profiles),
            "--max-candidates-per-article",
            str(args.max_candidates_per_article),
            "--max-news-events",
            str(args.max_news_events),
            "--batch-size",
            str(args.news_batch_size),
        ]
        if args.no_gdelt:
            news_command.append("--no-gdelt")
        processes.append(
            ManagedProcess(
                key="news_pipeline",
                label="News surveillance pipeline",
                command=news_command,
                cwd=root,
                log_dir=log_dir,
            )
        )
    if args.with_retention_maintenance:
        retention_command = [
            py,
            "-m",
            "scripts.run_retention_maintenance",
            "--watch",
            "--interval-seconds",
            str(args.retention_interval_seconds),
            "--book-event-days",
            str(args.retention_book_event_days),
            "--observe-snapshot-days",
            str(args.retention_observe_snapshot_days),
            "--sampled-snapshot-days",
            str(args.retention_sampled_snapshot_days),
            "--hot-snapshot-days",
            str(args.retention_hot_snapshot_days),
            "--batch-size",
            str(args.retention_batch_size),
            "--max-batches",
            str(args.retention_max_batches),
        ]
        if getattr(args, "retention_with_trade_retention", False):
            retention_command.extend(
                [
                    "--with-trade-retention",
                    "--trade-observe-days-after-close",
                    str(getattr(args, "retention_trade_observe_days_after_close", 1)),
                    "--trade-sampled-days-after-close",
                    str(getattr(args, "retention_trade_sampled_days_after_close", 14)),
                    "--trade-hot-days-after-close",
                    str(getattr(args, "retention_trade_hot_days_after_close", 30)),
                    "--trade-triggered-days-after-close",
                    str(getattr(args, "retention_trade_triggered_days_after_close", 90)),
                    "--trade-case-days-after-close",
                    str(getattr(args, "retention_trade_case_days_after_close", 365)),
                ]
            )
        if getattr(args, "retention_delete_flagged_trades_with_evidence", False):
            retention_command.append("--delete-flagged-trades-with-evidence")
        if getattr(args, "retention_allow_uncovered_trade_delete", False):
            retention_command.append("--allow-uncovered-trade-delete")
        if getattr(args, "retention_allow_uncovered_snapshot_delete", False):
            retention_command.append("--allow-uncovered-snapshot-delete")
        if getattr(args, "retention_exact_trade_dry_run_counts", False):
            retention_command.append("--exact-trade-dry-run-counts")
        if args.retention_execute:
            retention_command.append("--execute")
        if args.retention_analyze:
            retention_command.append("--analyze")
        processes.append(
            ManagedProcess(
                key="retention_maintenance",
                label="Retention maintenance",
                command=retention_command,
                cwd=root,
                log_dir=log_dir,
                forward_output=True,
            )
        )
    if args.with_anomaly_retention:
        anomaly_retention_command = [
            py,
            "-m",
            "scripts.run_anomaly_retention",
            "--watch",
            "--interval-seconds",
            str(args.anomaly_retention_interval_seconds),
            "--cutoff-days",
            str(args.anomaly_retention_cutoff_days),
            "--severities",
            args.anomaly_retention_severities,
            "--batch-size",
            str(args.anomaly_retention_batch_size),
            "--max-batches",
            str(args.anomaly_retention_max_batches),
            "--evidence-min-score",
            str(getattr(args, "anomaly_retention_evidence_min_score", 50.0)),
        ]
        if args.anomaly_retention_execute:
            anomaly_retention_command.append("--execute")
        if args.anomaly_retention_analyze:
            anomaly_retention_command.append("--analyze")
        processes.append(
            ManagedProcess(
                key="anomaly_retention",
                label="Anomaly retention",
                command=anomaly_retention_command,
                cwd=root,
                log_dir=log_dir,
                forward_output=True,
            )
        )
    if args.with_chart_history_compaction:
        chart_history_compaction_command = [
            py,
            "-m",
            "scripts.run_chart_history_compaction",
            "--watch",
            "--interval-seconds",
            str(args.chart_history_compaction_interval_seconds),
            "--grace-days-after-close",
            str(args.chart_history_compaction_grace_days_after_close),
            "--source-interval-sec",
            str(args.chart_history_compaction_source_interval_sec),
            "--target-interval-sec",
            str(args.chart_history_compaction_target_interval_sec),
            "--max-markets",
            str(args.chart_history_compaction_max_markets),
            "--offset",
            str(args.chart_history_compaction_offset),
            "--max-source-rows-per-market",
            str(args.chart_history_compaction_max_source_rows_per_market),
        ]
        if getattr(args, "chart_history_compaction_policies", ""):
            chart_history_compaction_command.extend(
                [
                    "--policies",
                    str(args.chart_history_compaction_policies),
                ]
            )
        if getattr(args, "chart_history_active_retention_policies", ""):
            chart_history_compaction_command.extend(
                [
                    "--active-retention-policies",
                    str(args.chart_history_active_retention_policies),
                ]
            )
        if args.chart_history_compaction_execute:
            chart_history_compaction_command.append("--execute")
        if args.chart_history_compaction_replace_source_rows:
            chart_history_compaction_command.append("--replace-source-rows")
        processes.append(
            ManagedProcess(
                key="chart_history_compaction",
                label="Chart history compaction",
                command=chart_history_compaction_command,
                cwd=root,
                log_dir=log_dir,
            )
        )
    if args.with_dashboard_cache_warmer:
        processes.append(
            ManagedProcess(
                key="dashboard_cache_warmer",
                label="Dashboard cache warmer",
                command=[
                    py,
                    "-m",
                    "scripts.warm_dashboard_cache",
                    "--watch",
                    "--interval-seconds",
                    str(args.dashboard_cache_warm_interval_seconds),
                    "--market-scopes",
                    args.dashboard_cache_warm_market_scopes,
                ],
                cwd=root,
                log_dir=log_dir,
            )
        )
    return processes


def _status_payload(processes: list[ManagedProcess]) -> dict[str, object]:
    return {
        proc.key: {
            "pid": proc.process.pid if proc.process is not None else None,
            "returncode": proc.poll(),
            "restarts": proc.restart_count,
        }
        for proc in processes
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=settings.app_host)
    parser.add_argument("--api-port", type=int, default=settings.app_port)
    parser.add_argument("--vite-port", type=int, default=5173)
    parser.add_argument("--log-dir", default=".pipeline-logs")
    parser.add_argument("--with-vite", action="store_true")
    parser.add_argument("--skip-api", action="store_true")
    parser.add_argument("--skip-poller", action="store_true")
    parser.add_argument("--skip-ws", action="store_true")
    parser.add_argument("--skip-news", action="store_true")
    parser.add_argument("--poll-interval-seconds", type=int, default=300)
    parser.add_argument("--poll-batch-size", type=int, default=500)
    parser.add_argument("--poll-error-backoff-seconds", type=float, default=300.0)
    parser.add_argument("--anomaly-market-limit", type=int, default=100)
    parser.add_argument("--hydrate-unknown-max", type=int, default=250)
    parser.add_argument("--refresh-active-lifecycle-max", type=int, default=25)
    parser.add_argument("--refresh-active-lifecycle-min-age-minutes", type=int, default=30)
    parser.add_argument("--refresh-active-lifecycle-sleep", type=float, default=0.02)
    parser.add_argument("--news-interval-seconds", type=float, default=300.0)
    parser.add_argument("--news-lookback-hours", type=int, default=168)
    parser.add_argument("--news-limit", type=int, default=500)
    parser.add_argument("--news-max-markets", type=int, default=12000)
    parser.add_argument("--news-max-profiles", type=int, default=12000)
    parser.add_argument("--max-candidates-per-article", type=int, default=150)
    parser.add_argument("--max-news-events", type=int, default=2000)
    parser.add_argument("--news-batch-size", type=int, default=100)
    parser.add_argument("--no-gdelt", action="store_true")
    parser.add_argument("--with-retention-maintenance", action="store_true")
    parser.add_argument("--retention-execute", action="store_true")
    parser.add_argument("--retention-analyze", action="store_true")
    parser.add_argument("--retention-interval-seconds", type=float, default=3600.0)
    parser.add_argument("--retention-book-event-days", type=int, default=7)
    parser.add_argument("--retention-observe-snapshot-days", type=int, default=2)
    parser.add_argument("--retention-sampled-snapshot-days", type=int, default=7)
    parser.add_argument("--retention-hot-snapshot-days", type=int, default=30)
    parser.add_argument("--retention-batch-size", type=int, default=2500)
    parser.add_argument("--retention-max-batches", type=int, default=60)
    parser.add_argument("--retention-with-trade-retention", action="store_true")
    parser.add_argument("--retention-trade-observe-days-after-close", type=int, default=1)
    parser.add_argument("--retention-trade-sampled-days-after-close", type=int, default=14)
    parser.add_argument("--retention-trade-hot-days-after-close", type=int, default=30)
    parser.add_argument("--retention-trade-triggered-days-after-close", type=int, default=90)
    parser.add_argument("--retention-trade-case-days-after-close", type=int, default=365)
    parser.add_argument(
        "--retention-delete-flagged-trades-with-evidence",
        action="store_true",
    )
    parser.add_argument(
        "--retention-allow-uncovered-trade-delete",
        action="store_true",
    )
    parser.add_argument(
        "--retention-allow-uncovered-snapshot-delete",
        action="store_true",
    )
    parser.add_argument("--retention-exact-trade-dry-run-counts", action="store_true")
    parser.add_argument("--with-anomaly-retention", action="store_true")
    parser.add_argument("--anomaly-retention-execute", action="store_true")
    parser.add_argument("--anomaly-retention-analyze", action="store_true")
    parser.add_argument("--anomaly-retention-interval-seconds", type=float, default=21600.0)
    parser.add_argument("--anomaly-retention-cutoff-days", type=int, default=7)
    parser.add_argument("--anomaly-retention-severities", default="none,low")
    parser.add_argument("--anomaly-retention-batch-size", type=int, default=5000)
    parser.add_argument("--anomaly-retention-max-batches", type=int, default=100)
    parser.add_argument("--anomaly-retention-evidence-min-score", type=float, default=50.0)
    parser.add_argument("--with-chart-history-compaction", action="store_true")
    parser.add_argument("--chart-history-compaction-execute", action="store_true")
    parser.add_argument(
        "--chart-history-compaction-replace-source-rows",
        action="store_true",
    )
    parser.add_argument(
        "--chart-history-compaction-interval-seconds",
        type=float,
        default=21_600.0,
    )
    parser.add_argument(
        "--chart-history-compaction-grace-days-after-close",
        type=int,
        default=7,
    )
    parser.add_argument(
        "--chart-history-compaction-source-interval-sec",
        type=int,
        default=DEFAULT_CHART_HISTORY_INTERVAL_SEC,
    )
    parser.add_argument(
        "--chart-history-compaction-target-interval-sec",
        type=int,
        default=CHART_HISTORY_COMPACTION_TARGET_INTERVAL_SEC,
    )
    parser.add_argument("--chart-history-compaction-max-markets", type=int, default=100)
    parser.add_argument("--chart-history-compaction-offset", type=int, default=0)
    parser.add_argument(
        "--chart-history-compaction-max-source-rows-per-market",
        type=int,
        default=0,
    )
    parser.add_argument("--chart-history-compaction-policies", default="")
    parser.add_argument("--chart-history-active-retention-policies", default="")
    parser.add_argument("--with-dashboard-cache-warmer", action="store_true")
    parser.add_argument("--dashboard-cache-warm-interval-seconds", type=float, default=60.0)
    parser.add_argument("--dashboard-cache-warm-market-scopes", default="active")
    args = parser.parse_args()

    run_id = new_run_id("supervisor")
    processes = build_processes(args)
    if not processes:
        raise SystemExit("No pipeline components selected.")
    if not args.with_retention_maintenance:
        record_pipeline_heartbeat(
            "retention_maintenance",
            status="paused",
            detail="Retention maintenance is disabled by supervisor args.",
            run_id=run_id,
            count=0,
            metadata={"enabled": False},
        )

    mark_pipeline_start(
        "pipeline_supervisor",
        detail="Starting local pipeline supervisor.",
        run_id=run_id,
        metadata={"components": [proc.key for proc in processes]},
    )
    for proc in processes:
        proc.start()

    stopping = False

    def _stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    try:
        last_heartbeat = 0.0
        while not stopping:
            for proc in processes:
                returncode = proc.poll()
                if returncode is None:
                    continue
                mark_pipeline_error(
                    "pipeline_supervisor",
                    f"{proc.label} exited with code {returncode}",
                    detail=f"Restarting {proc.label}.",
                    run_id=run_id,
                    metadata={"component": proc.key, "returncode": returncode},
                )
                proc.restart()

            now_m = time.monotonic()
            if now_m - last_heartbeat >= 15:
                record_pipeline_heartbeat(
                    "pipeline_supervisor",
                    detail=(
                        f"Supervising {len(processes)} local pipeline components "
                        f"on API port {args.api_port}."
                    ),
                    run_id=run_id,
                    count=len(processes),
                    metadata=_status_payload(processes),
                )
                last_heartbeat = now_m
            time.sleep(1.0)
    finally:
        for proc in processes:
            proc.stop()
        record_pipeline_heartbeat(
            "pipeline_supervisor",
            status="stopped",
            detail="Local pipeline supervisor stopped.",
            run_id=run_id,
            metadata=_status_payload(processes),
        )


if __name__ == "__main__":
    main()
