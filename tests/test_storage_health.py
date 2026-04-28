from app.services.storage_health import _pressure_status, storage_health_detail


def test_pressure_status_thresholds(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.storage_health.settings.storage_warning_used_ratio",
        0.80,
    )
    monkeypatch.setattr(
        "app.services.storage_health.settings.storage_error_used_ratio",
        0.90,
    )

    assert _pressure_status(0.50) == "healthy"
    assert _pressure_status(0.85) == "stale"
    assert _pressure_status(0.95) == "error"
    assert _pressure_status(None) == "empty"


def test_storage_health_detail_is_compact() -> None:
    snapshot = {
        "postgres": {
            "database_bytes": 2 * 1024**3,
            "largest_tables": [
                {"table": "book_events", "total_bytes": 1024**3},
            ],
        },
        "local_disk": {"used_ratio": 0.42},
        "clickhouse": {"status": "healthy"},
        "opensearch": {"status": "empty"},
    }

    detail = storage_health_detail(snapshot)

    assert "Postgres 2.00 GiB" in detail
    assert "local disk 42% used" in detail
    assert "largest table book_events 1.00 GiB" in detail
