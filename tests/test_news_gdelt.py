"""Unit tests for `app.services.news_gdelt` (no database, no HTTP)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.news_gdelt import (
    build_gdelt_query,
    choose_news_window,
    default_gdelt_window,
    tokenize_for_gdelt,
)


def test_tokenize_strips_filler() -> None:
    q = tokenize_for_gdelt("Will the Fed cut rates in March?")
    assert "Fed" in q
    assert "the" not in q.split()


def test_build_query_combines_title_subtitle() -> None:
    m = SimpleNamespace(
        title="Will Manchester win on Sunday?",
        subtitle="Premier League match odds",
        market_id="KXM-TEST-1",
    )
    q = build_gdelt_query(m)  # type: ignore[arg-type]
    assert "OR" in q
    assert "Manchester" in q


def test_default_window_30d() -> None:
    end = datetime(2026, 3, 1, tzinfo=timezone.utc)
    m = SimpleNamespace(
        open_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        close_time=end,
    )
    start, e = default_gdelt_window(m)  # type: ignore[arg-type]
    assert e == end
    assert (e - start).days == 30


def test_activity_window_focuses_on_trade() -> None:
    now = datetime.now(timezone.utc)
    t = now - timedelta(hours=6)
    m = SimpleNamespace(
        open_time=now - timedelta(days=30),
        close_time=None,
    )
    s, e, anchors = choose_news_window(
        m,  # type: ignore[arg-type]
        last_trade=t,
        last_flag=None,
        align="activity",
    )
    assert anchors["align"] == "activity"
    assert s < t
    assert e >= t
    assert (e - s).total_seconds() <= 8.5 * 24 * 3600


def test_activity_falls_back_when_no_events() -> None:
    m = SimpleNamespace(
        open_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        close_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    s, e, anchors = choose_news_window(
        m,  # type: ignore[arg-type]
        last_trade=None,
        last_flag=None,
        align="activity",
    )
    assert anchors["align"] == "default"
    assert (e - s).days == 30
