"""Shared active/historical market lifecycle helpers.

Kalshi status metadata can lag the real world for scheduled events. The
dashboard, QA scripts, and materializers should agree on when a market is still
live versus only useful for historical review.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, text

from app.db.models import Market

ACTIVE_MARKET_STATUSES = frozenset({"open", "active"})
SPORTS_MARKET_CATEGORIES = frozenset(
    {"sports_outcome", "sports_derivative", "sports_prop"}
)
SPORTS_EVENT_PREFIXES = (
    "KXUFC",
    "KXMMA",
    "KXBELLATOR",
    "KXPFL",
    "KXBOX",
    "KXNBA",
    "KXNFL",
    "KXMLB",
    "KXNHL",
    "KXTENNIS",
    "KXATP",
    "KXWTA",
    "KXSOCCER",
    "KXEPL",
    "KXMLS",
    "KXLALIGA",
    "KXLIGAMX",
    "KXUCL",
    "KXUEFA",
    "KXFIFA",
    "KXGOLF",
    "KXPGA",
    "KXMASTERS",
    "KXIPL",
    "KXPSL",
    "KXCRICKET",
)
EVENT_DATE_TOKEN_PATTERN = (
    r"\d{2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}"
)
EVENT_DATE_TOKEN_RE = re.compile(EVENT_DATE_TOKEN_PATTERN)
EVENT_DATE_STALE_AFTER = timedelta(hours=30)


def normalize_market_scope(market_scope: str | None) -> str:
    scope = (market_scope or "active").lower()
    if scope not in {"active", "historical", "all"}:
        return "active"
    return scope


def sports_event_stale_expr():
    """SQL expression for scheduled sports markets whose ticker date has aged out."""

    event_token = func.substring(func.upper(Market.market_id), EVENT_DATE_TOKEN_PATTERN)
    sports_market = or_(
        Market.category.in_(tuple(SPORTS_MARKET_CATEGORIES)),
        *[
            func.upper(Market.market_id).like(f"{prefix}%")
            for prefix in SPORTS_EVENT_PREFIXES
        ],
    )
    stale_cutoff = func.to_timestamp(event_token, "YYMONDD") + text(
        "interval '30 hours'"
    )
    return and_(
        sports_market,
        event_token.isnot(None),
        stale_cutoff <= func.now(),
    )


def market_scope_filters(market_scope: str | None) -> list:
    """SQLAlchemy filters for active/historical/all market scopes."""

    scope = normalize_market_scope(market_scope)
    status = func.lower(func.coalesce(Market.status, ""))
    stale_event = sports_event_stale_expr()
    active = and_(
        status.in_(tuple(ACTIVE_MARKET_STATUSES)),
        or_(Market.close_time.is_(None), Market.close_time > func.now()),
        ~stale_event,
    )
    if scope == "active":
        return [active]
    if scope == "historical":
        return [
            or_(
                status.notin_(tuple(ACTIVE_MARKET_STATUSES)),
                Market.close_time <= func.now(),
                stale_event,
            )
        ]
    return []


def scheduled_event_date_from_market_id(market_id: str | None) -> datetime | None:
    if not market_id:
        return None
    match = EVENT_DATE_TOKEN_RE.search(market_id.upper())
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(0), "%y%b%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def looks_like_scheduled_sports_market(market: Market) -> bool:
    category = market.category or ""
    market_id = (market.market_id or "").upper()
    return category in SPORTS_MARKET_CATEGORIES or any(
        market_id.startswith(prefix) for prefix in SPORTS_EVENT_PREFIXES
    )


def is_stale_scheduled_event_market(
    market: Market,
    *,
    now: datetime,
) -> bool:
    if not looks_like_scheduled_sports_market(market):
        return False
    event_date = scheduled_event_date_from_market_id(market.market_id)
    if event_date is None:
        return False
    return event_date + EVENT_DATE_STALE_AFTER <= now


def market_lifecycle(market: Market, *, now: datetime | None = None) -> str:
    """Return active/historical/other lifecycle label for display and filtering."""

    now = now or datetime.now(timezone.utc)
    status = (market.status or "").lower()
    close_time = market.close_time
    if close_time is not None:
        close_time = (
            close_time.replace(tzinfo=timezone.utc)
            if close_time.tzinfo is None
            else close_time.astimezone(timezone.utc)
        )
    if is_stale_scheduled_event_market(market, now=now):
        return "historical"
    if status in ACTIVE_MARKET_STATUSES and (
        close_time is None or close_time > now
    ):
        return "active"
    if status in {"unknown", "out_of_scope"}:
        return status
    if close_time is not None and close_time <= now:
        return "historical"
    if status and status not in ACTIVE_MARKET_STATUSES:
        return "historical"
    return "other"
