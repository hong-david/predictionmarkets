"""Durable, best-effort heartbeat writes for pipeline components."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import PipelineHeartbeat
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

COMPONENTS: dict[str, tuple[str, str]] = {
    "api": ("API", "process"),
    "market_poller": ("Market poller / hydration", "process"),
    "ws_trade_feed": ("WebSocket trade feed", "process"),
    "news_ingest": ("News ingest", "job"),
    "news_links": ("News links", "materializer"),
    "news_trade_correlations": ("News/trade correlations", "materializer"),
    "trade_flags": ("Trade flags", "materializer"),
    "quote_book_anomalies": ("Quote/book alerts", "materializer"),
    "retention_projection": ("Retention/storage-tier projection", "projection"),
    "storage_guardrails": ("Storage guardrails", "projection"),
    "retention_maintenance": ("Retention maintenance", "job"),
    "clickhouse_retention": ("ClickHouse TTL verification", "job"),
    "pipeline_supervisor": ("Pipeline supervisor", "process"),
}


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonish(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def record_pipeline_heartbeat(
    key: str,
    *,
    label: str | None = None,
    component_type: str | None = None,
    status: str = "running",
    detail: str | None = None,
    run_id: str | None = None,
    count: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    success: bool = False,
    error: BaseException | str | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
) -> bool:
    """Upsert one heartbeat row.

    This is intentionally best-effort. A missing migration or temporary DB
    issue should not stop the poller, WS feed, or materializers from doing
    their primary work.
    """

    default_label, default_type = COMPONENTS.get(key, (key, "job"))
    label = label or default_label
    component_type = component_type or default_type
    now = _utc_now()
    error_text = str(error)[:4000] if error is not None else None
    is_error = error is not None or status == "error"
    if is_error:
        status = "error"
    values = {
        "key": key,
        "label": label,
        "component_type": component_type,
        "status": status,
        "detail": detail,
        "run_id": run_id,
        "pid": os.getpid(),
        "count": count,
        "metadata_json": _jsonish(metadata),
        "started_at": now if status == "starting" else None,
        "last_heartbeat_at": now,
        "last_success_at": now if success else None,
        "last_error_at": now if is_error else None,
        "last_error": error_text,
        "heartbeat_count": 1,
        "success_count": 1 if success else 0,
        "error_count": 1 if is_error else 0,
        "updated_at": now,
    }
    db = session_factory()
    try:
        stmt = pg_insert(PipelineHeartbeat).values(**values)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={
                "label": excluded.label,
                "component_type": excluded.component_type,
                "status": excluded.status,
                "detail": excluded.detail,
                "run_id": func.coalesce(excluded.run_id, PipelineHeartbeat.run_id),
                "pid": excluded.pid,
                "count": func.coalesce(excluded.count, PipelineHeartbeat.count),
                "metadata_json": excluded.metadata_json,
                "started_at": func.coalesce(PipelineHeartbeat.started_at, now)
                if status != "starting"
                else excluded.started_at,
                "last_heartbeat_at": excluded.last_heartbeat_at,
                "last_success_at": func.coalesce(
                    excluded.last_success_at, PipelineHeartbeat.last_success_at
                ),
                "last_error_at": func.coalesce(
                    excluded.last_error_at, PipelineHeartbeat.last_error_at
                ),
                "last_error": func.coalesce(
                    excluded.last_error, PipelineHeartbeat.last_error
                ),
                "heartbeat_count": PipelineHeartbeat.heartbeat_count + 1,
                "success_count": PipelineHeartbeat.success_count
                + (1 if success else 0),
                "error_count": PipelineHeartbeat.error_count + (1 if is_error else 0),
                "updated_at": excluded.updated_at,
            },
        )
        db.execute(stmt)
        db.commit()
        return True
    except SQLAlchemyError as exc:
        db.rollback()
        logger.debug("pipeline heartbeat write skipped for %s: %s", key, exc)
        return False
    finally:
        db.close()


def mark_pipeline_start(
    key: str,
    *,
    detail: str | None = None,
    run_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    return record_pipeline_heartbeat(
        key,
        status="starting",
        detail=detail,
        run_id=run_id,
        metadata=metadata,
    )


def mark_pipeline_success(
    key: str,
    *,
    detail: str | None = None,
    run_id: str | None = None,
    count: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    return record_pipeline_heartbeat(
        key,
        status="healthy",
        detail=detail,
        run_id=run_id,
        count=count,
        metadata=metadata,
        success=True,
    )


def mark_pipeline_error(
    key: str,
    error: BaseException | str,
    *,
    detail: str | None = None,
    run_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    return record_pipeline_heartbeat(
        key,
        status="error",
        detail=detail,
        run_id=run_id,
        metadata=metadata,
        error=error,
    )
