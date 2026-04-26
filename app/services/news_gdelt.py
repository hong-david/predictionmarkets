"""GDELT news query + time window helpers (public data, no key).

`GET /api/dashboard/markets/{id}/news` calls into these; unit-tested without HTTP.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.models import Market

_GDELT_STOPWORDS = frozenset(
    {
        "will", "the", "a", "an", "of", "in", "on", "at", "to", "for", "with",
        "and", "or", "be", "by", "is", "are", "was", "were", "this", "that",
        "above", "below", "than", "more", "less", "vs", "vs.",
    }
)


def _naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def tokenize_for_gdelt(text: str | None, max_tokens: int = 8) -> str:
    if not text or not str(text).strip():
        return ""
    raw = str(text).rstrip("?").strip()
    raw = re.sub(r"[^\w\s.-]", " ", raw)
    parts = [t for t in raw.split() if t.lower() not in _GDELT_STOPWORDS and len(t) > 1]
    return " ".join(parts[:max_tokens])


def build_gdelt_query(market: Market) -> str:
    """Title + optional subtitle as an OR group when both differ (GDELT boolean syntax)."""
    t = tokenize_for_gdelt(market.title, 8)
    s = tokenize_for_gdelt(market.subtitle, 6)
    if t and s and t != s:
        return f"({t}) OR ({s})"
    return t or s or (market.market_id or "").strip()


def default_gdelt_window(market: Market) -> tuple[datetime, datetime]:
    """30-day window ending at close (or now if open), matching legacy behavior."""
    end = market.close_time or datetime.now(timezone.utc)
    end = _naive_utc(end) or datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    return start, end


def choose_news_window(
    market: Market,
    *,
    last_trade: datetime | None,
    last_flag: datetime | None,
    align: str,
) -> tuple[datetime, datetime, dict[str, Any]]:
    """
    Picks a GDELT [start, end) window and returns anchor metadata for the UI
    to compare against trades and materialized rule scores.
    """
    now = datetime.now(timezone.utc)
    lt = _naive_utc(last_trade)
    lf = _naive_utc(last_flag)
    op = _naive_utc(market.open_time)
    cl = _naive_utc(market.close_time)

    if align == "activity" and (lt or lf):
        candidates = [x for x in (lt, lf) if x is not None]
        focus = max(candidates)
        start = focus - timedelta(days=7)
        end = min(focus + timedelta(hours=12), now)
        if cl is not None:
            end = min(end, cl)
        if op is not None and start < op:
            start = op
        if end <= start:
            s, e = default_gdelt_window(market)
            return s, e, {
                "align": "default",
                "reason": "activity_window_empty_after_clamp",
                "last_trade_ts": lt.isoformat() if lt else None,
                "last_flag_ts": lf.isoformat() if lf else None,
            }
        return start, end, {
            "align": "activity",
            "last_trade_ts": lt.isoformat() if lt else None,
            "last_flag_ts": lf.isoformat() if lf else None,
            "focus_ts": focus.isoformat(),
        }

    s, e = default_gdelt_window(market)
    return s, e, {
        "align": "default",
        "last_trade_ts": lt.isoformat() if lt else None,
        "last_flag_ts": lf.isoformat() if lf else None,
    }
