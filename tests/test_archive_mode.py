from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app


def test_archive_status_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "archive_mode", False)
    monkeypatch.setattr(settings, "data_cutoff_at", "")

    response = TestClient(app).get("/api/archive-status")

    assert response.status_code == 200
    assert response.json() == {
        "archive_mode": False,
        "data_cutoff_at": None,
    }


def test_archive_status_exposes_cutoff(monkeypatch) -> None:
    monkeypatch.setattr(settings, "archive_mode", True)
    monkeypatch.setattr(settings, "data_cutoff_at", "2026-06-19T02:14:38Z")

    response = TestClient(app).get("/api/archive-status")

    assert response.status_code == 200
    assert response.json() == {
        "archive_mode": True,
        "data_cutoff_at": "2026-06-19T02:14:38Z",
    }
