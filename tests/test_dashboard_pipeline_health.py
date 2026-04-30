from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.routes.dashboard import (
    _component_from_heartbeat,
    _freshness_status,
    _pipeline_component,
    _pipeline_summary,
)
from app.db.models import PipelineHeartbeat


def test_pipeline_summary_healthy_when_all_components_healthy() -> None:
    components = [
        _pipeline_component(
            key="api",
            label="API",
            status="healthy",
            latest_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            age_seconds=0,
            count=None,
            description="API",
            detail="ok",
        ),
        _pipeline_component(
            key="trade_flags",
            label="Trade flags",
            status="healthy",
            latest_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            age_seconds=10,
            count=3,
            description="Flags",
            detail="ok",
        ),
    ]

    summary = _pipeline_summary(components)

    assert summary == {
        "status": "healthy",
        "healthy": 2,
        "stale": 0,
        "empty": 0,
        "error": 0,
        "total": 2,
    }


def test_pipeline_summary_empty_when_only_api_is_live() -> None:
    components = [
        {"key": "api", "status": "healthy"},
        {"key": "news_ingest", "status": "empty"},
        {"key": "news_links", "status": "empty"},
    ]

    summary = _pipeline_summary(components)

    assert summary["status"] == "empty"
    assert summary["healthy"] == 1
    assert summary["empty"] == 2


def test_freshness_status_uses_count_and_age() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    assert (
        _freshness_status(
            latest_at=now - timedelta(minutes=10),
            count=1,
            stale_after=timedelta(hours=1),
            now=now,
        )
        == "healthy"
    )
    assert (
        _freshness_status(
            latest_at=now - timedelta(hours=2),
            count=1,
            stale_after=timedelta(hours=1),
            now=now,
        )
        == "stale"
    )
    assert (
        _freshness_status(
            latest_at=now,
            count=0,
            stale_after=timedelta(hours=1),
            now=now,
        )
        == "empty"
    )


def test_component_from_heartbeat_prefers_recent_heartbeat() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    heartbeat = PipelineHeartbeat(
        key="trade_flags",
        label="Trade flags",
        component_type="materializer",
        status="healthy",
        detail="Last batch wrote 12 flags.",
        count=12,
        last_heartbeat_at=now - timedelta(minutes=2),
        last_success_at=now - timedelta(minutes=2),
    )

    payload = _component_from_heartbeat(
        heartbeat,
        key="trade_flags",
        label="Trade flags",
        description="Flags",
        db_latest_at=None,
        db_count=0,
        db_detail="No flags.",
        stale_after=timedelta(hours=24),
        now=now,
    )

    assert payload["status"] == "healthy"
    assert payload["source"] == "heartbeat"
    assert payload["count"] == 12
    assert payload["detail"] == "Last batch wrote 12 flags."


def test_component_from_heartbeat_can_treat_zero_success_as_healthy() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    heartbeat = PipelineHeartbeat(
        key="quote_book_anomalies",
        label="Quote/book alerts",
        component_type="materializer",
        status="healthy",
        detail="Scanned 50 markets; created 0.",
        count=0,
        last_heartbeat_at=now - timedelta(minutes=2),
        last_success_at=now - timedelta(minutes=2),
        last_error="old transient error",
    )

    payload = _component_from_heartbeat(
        heartbeat,
        key="quote_book_anomalies",
        label="Quote/book alerts",
        description="Quote/book alerts",
        db_latest_at=None,
        db_count=0,
        db_detail="No alert rows.",
        stale_after=timedelta(hours=24),
        now=now,
        zero_count_is_healthy=True,
    )

    assert payload["status"] == "healthy"
    assert payload["count"] == 0
    assert payload["last_error"] is None
