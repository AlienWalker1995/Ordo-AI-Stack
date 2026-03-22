"""Tests for GET /api/dependencies (M7)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    import dashboard.app as dashboard_app

    async def _stub_check(url: str):
        return (True, "")

    monkeypatch.setattr("dashboard.services_catalog._check_service", _stub_check)

    sample = {
        "version": 1,
        "description": "test",
        "entries": [
            {
                "id": "model-gateway",
                "ok": True,
                "latency_ms": 1.0,
                "error": None,
                "ready_ok": True,
                "ready_latency_ms": 2.0,
                "ready_error": None,
            }
        ],
    }
    # Patch where used — routes_hub binds probe_all at import time
    monkeypatch.setattr("dashboard.routes_hub.probe_all", lambda: sample)
    return TestClient(dashboard_app.app)


def test_dependencies_returns_200(client):
    """GET /api/dependencies returns 200 without auth."""
    r = client.get("/api/dependencies")
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == 1
    assert len(data["entries"]) == 1
    assert data["entries"][0]["id"] == "model-gateway"
    assert data["entries"][0]["ok"] is True
