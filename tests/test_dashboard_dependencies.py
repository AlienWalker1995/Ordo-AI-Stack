"""Tests for GET /api/dependencies (M7)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    import dashboard.app as dashboard_app

    async def _stub_check(url: str, client=None):
        return (True, "")

    monkeypatch.setattr("dashboard.services_catalog._check_service", _stub_check)

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr("dashboard.app._http_client", mock_client)

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
    # Patch where used — probe_all is now async
    async def _stub_probe_all(client=None):
        return sample
    monkeypatch.setattr("dashboard.routes_hub.probe_all", _stub_probe_all)
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
