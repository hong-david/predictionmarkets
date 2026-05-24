"""Materialize market-state alert rows into the legacy `anomalies` table."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import Anomaly, Market
from app.services.market_state_alert_engine import analyze_market
from app.services.book_activity_signals import collect_book_activity_signals
from app.services.market_metrics import bump_anomaly_metrics
from app.services.pipeline_heartbeat import mark_pipeline_success
from app.services.quote_series import history_points_for_market

# Do not store or refresh rows for weak scores; they dominated the market-detail
# chart. ~3.0 is a single "medium" rule firing with headroom, or a few stacked
# low signals. Tune alongside `market_state_alert_engine` thresholds.
_MIN_SCORE_TO_PERSIST = 3.0
_COMPACTION_COOLDOWN = timedelta(minutes=30)
_SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _severity_rank(value: object) -> int:
    return _SEVERITY_RANK.get(str(value or "").lower(), 0)


def _reason_signature(reasons: object) -> tuple[str, ...]:
    if not isinstance(reasons, list):
        return ()
    return tuple(sorted({str(reason) for reason in reasons}))


def _should_create_new_row(
    existing: Anomaly | None,
    *,
    severity: str,
    now: datetime,
    cooldown: timedelta = _COMPACTION_COOLDOWN,
) -> bool:
    """Return true only for an alert transition worth preserving as history."""

    if existing is None:
        return True
    if _severity_rank(severity) > _severity_rank(existing.severity):
        return True
    created_at = _aware(existing.created_at)
    if created_at is None:
        return False
    return now - created_at >= cooldown


def materialize_market_anomaly(
    db: Session,
    market: Market,
    lookback: int = 40,
    latest_snapshot_id: int | None = None,
) -> dict[str, int]:
    """Score and persist one market-state alert if the latest state warrants it."""
    snapshots = history_points_for_market(
        db,
        int(market.id),
        limit=lookback,
        newest_first=True,
    )

    if not snapshots:
        return {
            "created_anomalies": 0,
            "updated_anomalies": 0,
            "deleted_anomalies": 0,
            "compacted_anomalies": 0,
        }

    book_raw = collect_book_activity_signals(db, market.id)
    analysis = analyze_market(market, snapshots, book_activity=book_raw)
    effective_latest_snapshot_id = latest_snapshot_id
    latest_quote = snapshots[0]
    latest_quote_key = getattr(latest_quote, "source_key", None)
    score = float(analysis["score"])

    existing = None
    if effective_latest_snapshot_id is not None:
        existing = (
            db.query(Anomaly)
            .filter(
                Anomaly.market_pk == market.id,
                Anomaly.latest_snapshot_id == effective_latest_snapshot_id,
            )
            .one_or_none()
        )
    if existing is None and latest_quote_key is not None:
        latest_existing = (
            db.query(Anomaly)
            .filter(Anomaly.market_pk == market.id)
            .order_by(Anomaly.created_at.desc(), Anomaly.id.desc())
            .first()
        )
        if (
            latest_existing is not None
            and (latest_existing.signals or {}).get("latest_quote_source_key")
            == latest_quote_key
        ):
            existing = latest_existing

    if score < _MIN_SCORE_TO_PERSIST:
        if existing is not None:
            db.delete(existing)
            return {
                "created_anomalies": 0,
                "updated_anomalies": 0,
                "deleted_anomalies": 1,
                "compacted_anomalies": 0,
            }
        return {
            "created_anomalies": 0,
            "updated_anomalies": 0,
            "deleted_anomalies": 0,
            "compacted_anomalies": 0,
        }

    if existing is not None:
        existing.score = analysis["score"]
        existing.severity = analysis["severity"]
        existing.reasons = analysis["reasons"]
        existing.signals = analysis["signals"]
        return {
            "created_anomalies": 0,
            "updated_anomalies": 1,
            "deleted_anomalies": 0,
            "compacted_anomalies": 0,
        }

    latest_existing = (
        db.query(Anomaly)
        .filter(Anomaly.market_pk == market.id)
        .order_by(Anomaly.created_at.desc(), Anomaly.id.desc())
        .first()
    )
    if not _should_create_new_row(
        latest_existing,
        severity=str(analysis["severity"]),
        now=datetime.now(timezone.utc),
    ):
        latest_existing.latest_snapshot_id = effective_latest_snapshot_id
        latest_existing.score = analysis["score"]
        latest_existing.severity = analysis["severity"]
        latest_existing.reasons = analysis["reasons"]
        latest_existing.signals = {
            **(analysis["signals"] or {}),
            "compacted_from_latest_snapshot_id": effective_latest_snapshot_id,
            "compacted_from_latest_quote_source_key": latest_quote_key,
            "reason_signature": list(_reason_signature(analysis["reasons"])),
        }
        return {
            "created_anomalies": 0,
            "updated_anomalies": 1,
            "deleted_anomalies": 0,
            "compacted_anomalies": 1,
        }

    row = Anomaly(
        market_pk=market.id,
        latest_snapshot_id=effective_latest_snapshot_id,
        score=analysis["score"],
        severity=analysis["severity"],
        reasons=analysis["reasons"],
        signals=analysis["signals"],
    )
    db.add(row)
    bump_anomaly_metrics(
        db,
        market_pk=market.id,
        anomaly_ts=datetime.now(timezone.utc),
        prior=market.manipulability_prior,
        severity=str(analysis["severity"]),
    )
    return {
        "created_anomalies": 1,
        "updated_anomalies": 0,
        "deleted_anomalies": 0,
        "compacted_anomalies": 0,
    }


def materialize_anomalies(
    db: Session,
    market_limit: int = 100,
    lookback: int = 40,
) -> dict[str, int]:
    created = 0
    updated = 0
    deleted = 0
    compacted = 0

    markets = db.query(Market).order_by(Market.id.desc()).limit(market_limit).all()

    for market in markets:
        result = materialize_market_anomaly(db, market, lookback=lookback)
        created += result["created_anomalies"]
        updated += result["updated_anomalies"]
        deleted += result["deleted_anomalies"]
        compacted += result["compacted_anomalies"]

    db.commit()
    mark_pipeline_success(
        "quote_book_anomalies",
        detail=(
            f"Scanned {len(markets)} markets; created {created}, updated {updated}, "
            f"compacted {compacted}, deleted {deleted}."
        ),
        count=created + updated,
        metadata={
            "scanned_markets": len(markets),
            "created": created,
            "updated": updated,
            "compacted": compacted,
            "deleted": deleted,
        },
    )

    return {
        "created_anomalies": created,
        "updated_anomalies": updated,
        "deleted_anomalies": deleted,
        "compacted_anomalies": compacted,
    }
