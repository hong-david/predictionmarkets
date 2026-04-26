"""Order-book *activity* summaries for anomaly scoring.

We do not yet reconstruct a full L2 book in memory; we use cheap counts and
aggregate cancel flow on recent `book_events` rows. High delta rate can
indicate layering / flicker without a full spoofing detector."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import BookEvent


def collect_book_activity_signals(db: Session, market_pk: int) -> dict[str, Any]:
    """Last ~3 minutes of book events for this market (by `received_at`)."""
    since = datetime.now(timezone.utc) - timedelta(minutes=3)

    total = (
        db.query(func.count(BookEvent.id))
        .filter(BookEvent.market_pk == market_pk, BookEvent.received_at >= since)
        .scalar()
        or 0
    )

    delta_rows = (
        db.query(func.count(BookEvent.id))
        .filter(
            BookEvent.market_pk == market_pk,
            BookEvent.received_at >= since,
            BookEvent.is_snapshot.is_(False),
        )
        .scalar()
        or 0
    )

    neg_sum = (
        db.query(func.coalesce(func.sum(BookEvent.delta_fp), 0))
        .filter(
            BookEvent.market_pk == market_pk,
            BookEvent.received_at >= since,
            BookEvent.is_snapshot.is_(False),
            BookEvent.delta_fp < 0,
        )
        .scalar()
    )
    neg_contracts = float(neg_sum) if neg_sum is not None else 0.0
    neg_contracts = abs(neg_contracts)  # display as positive "pulled size"

    return {
        "window_minutes": 3,
        "book_events_3m": int(total),
        "book_deltas_3m": int(delta_rows),
        "pull_volume_3m": neg_contracts,
        "high_churn": total > 400,
        "heavy_cancel_flex": neg_contracts > 75.0 and delta_rows > 30,
    }


def book_signals_for_scoring(s: dict[str, Any]) -> tuple[float, list[str], dict[str, Any]]:
    """Turn raw counts into score bump + reasons. Returns (points, reasons, signals_out)."""
    score = 0.0
    reasons: list[str] = []
    out = dict(s)

    if s.get("high_churn"):
        score += 1.5
        reasons.append("very high order-book event rate (3m)")
    if s.get("heavy_cancel_flex"):
        score += 2.0
        reasons.append("sustained cancel / pull side on book deltas")

    out["book_score_component"] = round(score, 2)
    return score, reasons, out
