"""Smoke tests for the dashboard JSON API.

These exercise the FastAPI app via TestClient against the configured
Postgres database. Like `test_migrations_integration.py`, the file is
gated on RUN_INTEGRATION=1 because it needs a real DB connection.

The goal is shape-checking, not data validation: each endpoint should
return the documented JSON contract so the frontend in `frontend/`
keeps compiling and rendering. We assert on the *structure* of the
response, not the specific numbers (which depend on what's in the test
DB).

Pure-function pieces of `dashboard.py` (the news query builder, the
prior-rank case expression) are exercised here implicitly. If they
broke, the endpoints would 500.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="Integration test; set RUN_INTEGRATION=1 to enable.",
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_health_under_api_prefix(client: TestClient) -> None:
    """The /api/* prefix is what the frontend's Vite proxy expects.
    Regression guard for the day someone helpfully renames it."""
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_overview_bundles_expected_keys(client: TestClient) -> None:
    r = client.get("/api/dashboard/overview", params={"top": 2, "anomalies": 2})
    assert r.status_code == 200
    body = r.json()
    assert {"stats", "breakdown", "top_markets", "recent_anomalies"} <= body.keys()
    assert "markets" in body["stats"] and "by_prior" in body["breakdown"]


def test_stats_returns_expected_keys(client: TestClient) -> None:
    r = client.get("/api/dashboard/stats")
    assert r.status_code == 200
    body = r.json()
    expected = {
        "market_scope",
        "markets",
        "markets_all",
        "markets_active",
        "markets_historical",
        "markets_status_unknown",
        "markets_high_prior",
        "markets_with_flags",
        "trades",
        "snapshots",
        "book_events",
        "anomalies",
        "news_articles",
        "anomalies_high_severity",
    }
    assert expected <= body.keys()
    assert body["market_scope"] in {"active", "historical", "all"}
    for k in expected - {"market_scope"}:
        assert isinstance(body[k], int) and body[k] >= 0


def test_breakdown_returns_pivots(client: TestClient) -> None:
    r = client.get("/api/dashboard/breakdown")
    assert r.status_code == 200
    body = r.json()
    for axis in ("by_category", "by_prior", "by_confidence", "by_layer"):
        assert isinstance(body[axis], list)
        for entry in body[axis]:
            assert "key" in entry and "count" in entry
            assert isinstance(entry["count"], int)

    assert "category_x_prior" in body
    for entry in body["category_x_prior"]:
        assert {"category", "prior", "count"} <= entry.keys()


def test_pipeline_health_returns_component_statuses(client: TestClient) -> None:
    r = client.get("/api/dashboard/pipeline-health")
    assert r.status_code == 200
    body = r.json()
    assert {"generated_at", "summary", "components"} <= body.keys()
    assert body["summary"]["total"] == len(body["components"])
    assert body["summary"]["status"] in {"healthy", "degraded", "empty", "error"}
    expected_keys = {
        "api",
        "market_poller",
        "ws_trade_feed",
        "news_ingest",
        "news_links",
        "news_trade_correlations",
        "trade_flags",
        "quote_book_anomalies",
        "retention_projection",
        "storage_guardrails",
    }
    assert expected_keys == {c["key"] for c in body["components"]}
    for component in body["components"]:
        assert {
            "key",
            "label",
            "status",
            "latest_at",
            "age_seconds",
            "count",
            "description",
            "detail",
        } <= component.keys()
        assert component["status"] in {"healthy", "stale", "empty", "error"}


def test_markets_list_pagination_and_filters(client: TestClient) -> None:
    """Pagination, filtering, and sort all work."""
    r = client.get("/api/dashboard/markets", params={"limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert {"total", "filtered", "limit", "offset", "markets"} <= body.keys()
    assert body["limit"] == 5
    assert body["offset"] == 0
    assert isinstance(body["markets"], list)
    assert len(body["markets"]) <= 5

    if body["markets"]:
        m = body["markets"][0]
        assert {
            "market_id",
            "title",
            "status",
            "manipulability_prior",
            "trade_count",
            "anomaly_count",
            "market_priority",
            "evidence_score",
            "urgency_score",
            "top_trade_flag_score",
            "reasons",
        } <= m.keys()

    r2 = client.get(
        "/api/dashboard/markets",
        params={"sort": "trades_desc", "limit": 5, "category": "sports_outcome"},
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["filtered"] <= body2["total"]
    for m in body2["markets"]:
        assert m["category"] == "sports_outcome"

    r_sort = client.get(
        "/api/dashboard/markets",
        params={"sort": "top_trade_flag", "limit": 5},
    )
    assert r_sort.status_code == 200
    for m in r_sort.json()["markets"]:
        assert "top_trade_flag_score" in m

    # NULL category in DB is shown as the string "unclassified" in
    # breakdown charts; the list filter must match the same rule.
    r3 = client.get(
        "/api/dashboard/markets",
        params={"category": "unclassified", "limit": 5},
    )
    assert r3.status_code == 200
    for m in r3.json()["markets"]:
        assert m["category"] is None


def test_market_detail_404_for_missing_id(client: TestClient) -> None:
    r = client.get("/api/dashboard/markets/__definitely_does_not_exist__")
    assert r.status_code == 404
    assert r.json()["detail"] == "Market not found"


def test_market_detail_returns_stats_and_classifier(client: TestClient) -> None:
    """End-to-end: pick the top-traded market and verify the detail
    bundle has all the fields the frontend reads. We use top-markets
    rather than hard-coding a ticker so the test survives data churn.
    """
    top = client.get("/api/dashboard/top-markets", params={"limit": 1}).json()
    if not top["markets"]:
        pytest.skip("no markets ingested yet")
    market_id = top["markets"][0]["market_id"]

    r = client.get(f"/api/dashboard/markets/{market_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["market_id"] == market_id
    assert "stats" in body
    assert {
        "trade_count",
        "first_trade_ts",
        "last_trade_ts",
        "min_yes_price",
        "max_yes_price",
    } <= body["stats"].keys()
    assert "latest_snapshot" in body
    assert "classifier_tags" in body
    assert "market_priority" in body
    assert "evidence_score" in body
    assert "urgency_score" in body
    assert "reasons" in body


def test_market_series_shape(client: TestClient) -> None:
    top = client.get("/api/dashboard/top-markets", params={"limit": 1}).json()
    if not top["markets"]:
        pytest.skip("no markets ingested yet")
    market_id = top["markets"][0]["market_id"]

    r = client.get(
        f"/api/dashboard/markets/{market_id}/series",
        params={"limit": 100},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["market_id"] == market_id
    assert "tape_cluster" in body
    assert {
        "burst_score_0_10",
        "largest_window_count",
        "window_sec",
        "dominant_side",
    } <= body["tape_cluster"].keys()
    assert isinstance(body["trades"], list)
    assert isinstance(body["snapshots"], list)
    for t in body["trades"]:
        assert {"ts", "yes_price", "count", "taker_side", "trade_dollar_amount"} <= t.keys()
        if "suspicion" in t and t["suspicion"] is not None:
            assert 0.0 <= float(t["suspicion"]) <= 10.0
        if "cluster_0_10" in t and t["cluster_0_10"] is not None:
            assert 0.0 <= float(t["cluster_0_10"]) <= 10.0


def test_news_endpoint_returns_payload_shape(client: TestClient) -> None:
    """News should always return a well-formed payload, even when GDELT
    is unreachable (then `provider == "unavailable"`). The frontend
    relies on this so it can render an empty state without erroring."""
    top = client.get("/api/dashboard/top-markets", params={"limit": 1}).json()
    if not top["markets"]:
        pytest.skip("no markets ingested yet")
    market_id = top["markets"][0]["market_id"]

    r = client.get(f"/api/dashboard/markets/{market_id}/news", params={"limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert {"market_id", "query", "since", "until", "provider", "articles"} <= body.keys()
    assert body["provider"] in {"gdelt", "unavailable"}
    assert isinstance(body["articles"], list)


def test_unknown_api_path_returns_404_not_spa(client: TestClient) -> None:
    """The catch-all SPA route in `app/main.py` must skip /api/* paths
    so a typo'd API URL still returns a JSON 404 instead of swallowing
    the request and returning index.html."""
    r = client.get("/api/this_route_does_not_exist")
    assert r.status_code == 404
    # Whatever 404 body we return, it must not be HTML (which would
    # mean the SPA fallback ate the request).
    assert "text/html" not in r.headers.get("content-type", "")


def test_unknown_non_api_path_serves_spa_or_placeholder(client: TestClient) -> None:
    """Non-API paths fall through to the SPA serve_frontend handler."""
    r = client.get("/markets/some_random_path/that_does_not_exist")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
