"""Warm dashboard Redis cache for first-paint API endpoints."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import time

from app.api.routes.dashboard import warm_dashboard_cache_once
from app.db.session import SessionLocal
from app.services.pipeline_heartbeat import (
    mark_pipeline_error,
    mark_pipeline_start,
    mark_pipeline_success,
    new_run_id,
    record_pipeline_heartbeat,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_scopes(raw: str) -> tuple[str, ...]:
    scopes = tuple(s.strip() for s in raw.split(",") if s.strip())
    return scopes or ("active",)


def run_once(scopes: tuple[str, ...], *, run_id: str | None = None) -> dict:
    db = SessionLocal()
    try:
        result = warm_dashboard_cache_once(db, market_scopes=scopes)
        mark_pipeline_success(
            "dashboard_cache_warmer",
            detail=(
                f"Warmed {result['count']} dashboard cache entries; "
                f"{result['error_count']} errors."
            ),
            run_id=run_id,
            count=int(result["count"]),
            metadata=result,
        )
        return result
    except Exception as exc:
        mark_pipeline_error(
            "dashboard_cache_warmer",
            exc,
            detail="Dashboard cache warm failed.",
            run_id=run_id,
        )
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--market-scopes",
        default="active",
        help="Comma-separated dashboard market scopes to warm: active,historical,all.",
    )
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    args = parser.parse_args()

    scopes = _parse_scopes(args.market_scopes)
    run_id = new_run_id("dashboard-cache")
    mark_pipeline_start(
        "dashboard_cache_warmer",
        detail=f"Starting dashboard cache warmer for scopes={','.join(scopes)}.",
        run_id=run_id,
    )

    if args.watch:
        print(
            f"[{_utc_now()}] dashboard cache warmer starting "
            f"interval={args.interval_seconds}s scopes={','.join(scopes)}",
            flush=True,
        )
        while True:
            record_pipeline_heartbeat(
                "dashboard_cache_warmer",
                detail="Dashboard cache warmer watch loop alive.",
                run_id=run_id,
            )
            print(json.dumps(run_once(scopes, run_id=run_id), sort_keys=True), flush=True)
            time.sleep(args.interval_seconds)

    print(json.dumps(run_once(scopes, run_id=run_id), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
