from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.models import Anomaly
from app.services.anomaly_materializer import (
    _reason_signature,
    _should_create_new_row,
)


def _row(*, severity: str, created_at: datetime) -> Anomaly:
    return Anomaly(
        market_pk=1,
        latest_snapshot_id=1,
        score=3.5,
        severity=severity,
        reasons=["wide_spread"],
        signals={},
        created_at=created_at,
    )


def test_anomaly_compaction_creates_first_row() -> None:
    assert _should_create_new_row(
        None,
        severity="medium",
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_anomaly_compaction_creates_on_severity_upgrade() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    existing = _row(severity="medium", created_at=now)

    assert _should_create_new_row(existing, severity="high", now=now)


def test_anomaly_compaction_updates_inside_cooldown_without_upgrade() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    existing = _row(severity="high", created_at=now - timedelta(minutes=5))

    assert not _should_create_new_row(existing, severity="medium", now=now)


def test_anomaly_compaction_samples_after_cooldown() -> None:
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    existing = _row(severity="medium", created_at=now - timedelta(minutes=45))

    assert _should_create_new_row(existing, severity="medium", now=now)


def test_reason_signature_is_stable_and_deduped() -> None:
    assert _reason_signature(["volume", "spread", "volume"]) == (
        "spread",
        "volume",
    )
