from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.api.routes import dashboard
from app.api.routes.dashboard import (
    _cached_dashboard_payload,
    _clickhouse_table_count,
    _DASHBOARD_CACHE_SCHEMA_VERSION,
    _metric_probability_float,
    _probability_float,
)
from app.services.market_lifecycle import (
    market_lifecycle,
    scheduled_event_date_from_market_id,
)


def _market(
    *,
    market_id: str,
    category: str | None = "sports_outcome",
    status: str = "active",
    close_time: datetime | None = None,
):
    return SimpleNamespace(
        market_id=market_id,
        category=category,
        status=status,
        close_time=close_time,
    )


def test_stale_scheduled_sports_market_is_historical() -> None:
    market = _market(
        market_id="KXUFCFIGHT-26APR25STEZAL-STE",
        close_time=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )

    assert (
        market_lifecycle(
            market,
            now=datetime(2026, 4, 28, 18, tzinfo=timezone.utc),
        )
        == "historical"
    )


def test_sports_ticker_prefix_overrides_bad_category_for_lifecycle() -> None:
    market = _market(
        market_id="KXLIGAMXGAME-26APR25AMEATL-AME",
        category="election",
        close_time=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )

    assert (
        market_lifecycle(
            market,
            now=datetime(2026, 4, 28, 18, tzinfo=timezone.utc),
        )
        == "historical"
    )


def test_cricket_prefix_overrides_bad_category_for_lifecycle() -> None:
    market = _market(
        market_id="KXPSLGAME-26APR26RAWHYD-HYD",
        category="macro",
        close_time=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )

    assert (
        market_lifecycle(
            market,
            now=datetime(2026, 4, 28, 18, tzinfo=timezone.utc),
        )
        == "historical"
    )


def test_prior_day_scheduled_sports_market_is_historical_after_short_grace() -> None:
    market = _market(
        market_id="KXUCLGAME-26APR28PSGBMU-BMU",
        close_time=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )

    assert (
        market_lifecycle(
            market,
            now=datetime(2026, 4, 29, 8, tzinfo=timezone.utc),
        )
        == "historical"
    )


def test_prior_day_scheduled_non_sports_market_is_historical_after_short_grace() -> None:
    market = _market(
        market_id="KXKINGMENTION-26APR28C-QUEEN",
        category="politics",
        close_time=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )

    assert (
        market_lifecycle(
            market,
            now=datetime(2026, 4, 30, 8, tzinfo=timezone.utc),
        )
        == "historical"
    )


def test_invalid_date_like_ticker_fragment_is_ignored_for_lifecycle() -> None:
    assert scheduled_event_date_from_market_id("KXODD-26MAY47-YES") is None


def test_same_day_scheduled_sports_market_can_still_be_active() -> None:
    market = _market(
        market_id="KXATPMATCH-26APR28TSIRUU-RUU",
        close_time=datetime(2026, 5, 12, tzinfo=timezone.utc),
    )

    assert (
        market_lifecycle(
            market,
            now=datetime(2026, 4, 28, 18, tzinfo=timezone.utc),
        )
        == "active"
    )


def test_probability_values_are_clamped_for_chart_payloads() -> None:
    assert _probability_float(-0.2) == 0.0
    assert _probability_float(1.4) == 1.0
    assert _probability_float(0.42) == 0.42
    assert _probability_float(None) is None


def test_metric_probability_uses_latest_or_bid_ask_midpoint() -> None:
    assert (
        _metric_probability_float(
            last_price_cents=42,
            yes_bid_cents=30,
            yes_ask_cents=40,
        )
        == 0.42
    )
    assert (
        _metric_probability_float(
            last_price_cents=None,
            yes_bid_cents=30,
            yes_ask_cents=40,
        )
        == 0.35
    )


def test_clickhouse_table_count_reads_supported_table(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeResponse:
        text = "42\n"

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *, timeout, auth) -> None:
            calls.append({"timeout": timeout, "auth": auth})

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url, *, params):
            calls.append({"url": url, "params": params})
            return FakeResponse()

    monkeypatch.setattr(dashboard.httpx, "Client", FakeClient)
    monkeypatch.setattr(dashboard, "clickhouse_http_auth", lambda: ("user", "pass"))

    assert _clickhouse_table_count("kalshi_l2_events_raw") == 42
    assert calls[0]["auth"] == ("user", "pass")
    assert calls[1]["params"]["query"] == "SELECT count() FROM kalshi_l2_events_raw"


def test_clickhouse_table_count_rejects_unknown_table() -> None:
    try:
        _clickhouse_table_count("not_a_table")
    except ValueError as exc:
        assert "unsupported ClickHouse count table" in str(exc)
    else:
        raise AssertionError("expected unsupported table to raise")


def _pop_local_dashboard_cache(key: str) -> None:
    cache_key = f"{_DASHBOARD_CACHE_SCHEMA_VERSION}:{key}"
    with dashboard._dashboard_cache_lock:
        dashboard._dashboard_cache.pop(cache_key, None)


class _RecordingRedis:
    """Minimal Redis stub for `_cached_dashboard_payload` tests."""

    def __init__(self, initial: dict[str, bytes] | None = None) -> None:
        self._data = dict(initial or {})
        self.get_calls = 0
        self.setex_calls: list[tuple[str, int, bytes]] = []

    def get(self, key: str) -> bytes | None:
        self.get_calls += 1
        return self._data.get(key)

    def setex(self, key: str, ttl: int, value: bytes) -> None:
        self.setex_calls.append((key, ttl, value))
        self._data[key] = value


def test_cached_dashboard_redis_hit_returns_stale_without_rebuilding(
    monkeypatch,
) -> None:
    import orjson

    key = "z_unit_redis_hit"
    redis_key = f"dashboard:{_DASHBOARD_CACHE_SCHEMA_VERSION}:{key}"
    _pop_local_dashboard_cache(key)
    stale = {"generation": 1}
    fake = _RecordingRedis(initial={redis_key: orjson.dumps(stale)})
    monkeypatch.setattr(dashboard, "_dashboard_redis", lambda: fake)
    builds: list[int] = []

    def build() -> dict:
        builds.append(1)
        return {"generation": 2}

    out = _cached_dashboard_payload(key, build, ttl_sec=300)
    assert out == stale
    assert builds == []
    assert fake.get_calls == 1
    assert fake.setex_calls == []


def test_cached_dashboard_force_refresh_rebuilds_even_when_redis_has_key(
    monkeypatch,
) -> None:
    import orjson

    key = "z_unit_force_refresh"
    redis_key = f"dashboard:{_DASHBOARD_CACHE_SCHEMA_VERSION}:{key}"
    _pop_local_dashboard_cache(key)
    fake = _RecordingRedis(
        initial={redis_key: orjson.dumps({"generation": 1})},
    )
    monkeypatch.setattr(dashboard, "_dashboard_redis", lambda: fake)
    builds: list[int] = []

    def build() -> dict:
        builds.append(1)
        return {"generation": 2}

    out = _cached_dashboard_payload(
        key, build, ttl_sec=60, force_refresh=True,
    )
    assert out == {"generation": 2}
    assert builds == [1]
    assert fake.get_calls == 0
    assert len(fake.setex_calls) == 1
    assert fake.setex_calls[0][0] == redis_key
    assert orjson.loads(fake.setex_calls[0][2]) == {"generation": 2}
