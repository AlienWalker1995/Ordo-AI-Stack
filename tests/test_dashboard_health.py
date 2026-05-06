"""Test dashboard /api/health endpoint."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    """Stub service probes — real checks hit Docker DNS hostnames and are slow/flaky without a running stack."""
    from unittest.mock import AsyncMock, MagicMock

    import dashboard.app as dashboard_app

    async def _stub_check(url: str, client=None):
        return (True, "")

    monkeypatch.setattr("dashboard.services_catalog._check_service", _stub_check)

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr("dashboard.app._http_client", mock_client)
    return TestClient(dashboard_app.app)


def test_health_returns_200(client):
    """GET /api/health returns 200."""
    r = client.get("/api/health")
    assert r.status_code == 200


def test_health_has_ok_and_services(client):
    """GET /api/health returns ok boolean and services array."""
    r = client.get("/api/health")
    data = r.json()
    assert "ok" in data
    assert isinstance(data["ok"], bool)
    assert "services" in data
    assert isinstance(data["services"], list)
