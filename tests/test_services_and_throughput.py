"""Tests for /api/services, /api/throughput/*, /api/ollama/library, and global exception handler."""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    import dashboard.app as dashboard_app

    async def _stub_check(url: str, client=None):
        return (True, "")

    monkeypatch.setattr("dashboard.services_catalog._check_service", _stub_check)

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr("dashboard.app._http_client", mock_client)
    monkeypatch.setattr(dashboard_app, "_AUTH_REQUIRED", False)
    return TestClient(dashboard_app.app)


# ── /api/services ────────────────────────────────────────────────────────────

def test_services_returns_all_services(client):
    r = client.get("/api/services")
    assert r.status_code == 200
    data = r.json()
    assert "services" in data
    services = data["services"]
    assert len(services) >= 7
    ids = [s["id"] for s in services]
    assert "llamacpp" in ids
    assert "model-gateway" in ids


def test_services_have_required_fields(client):
    r = client.get("/api/services")
    for svc in r.json()["services"]:
        assert "id" in svc
        assert "name" in svc
        assert "port" in svc
        assert "ok" in svc
        assert "hint" in svc


def test_services_do_not_leak_auth_token(client, monkeypatch):
    """Regression: sensitive auth tokens must not appear in public /api/services URLs."""
    monkeypatch.setattr("dashboard.settings.DASHBOARD_AUTH_TOKEN", "secret-test-token-1234")
    # Re-import to pick up monkeypatched value
    import importlib
    import dashboard.services_catalog
    importlib.reload(dashboard.services_catalog)
    try:
        for svc in dashboard.services_catalog.SERVICES:
            assert "secret-test-token-1234" not in svc.get("url", ""), \
                f"Token leaked in service {svc['id']} URL: {svc['url']}"
    finally:
        importlib.reload(dashboard.services_catalog)


# ── /api/ollama/library ──────────────────────────────────────────────────────

def test_ollama_library_returns_models(client):
    r = client.get("/api/ollama/library")
    assert r.status_code == 200
    data = r.json()
    assert "models" in data
    assert data["ok"] is True
    assert isinstance(data["models"], list)
    assert len(data["models"]) > 0


# ── /api/throughput/record ───────────────────────────────────────────────────

def test_throughput_record_accepts_sample(client):
    r = client.post("/api/throughput/record", json={
        "model": "test-model",
        "output_tokens_per_sec": 25.5,
        "service": "test-svc",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_throughput_record_ignores_zero_tps(client):
    r = client.post("/api/throughput/record", json={
        "model": "test-model",
        "output_tokens_per_sec": 0,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_throughput_record_ignores_empty_model(client):
    r = client.post("/api/throughput/record", json={
        "model": "",
        "output_tokens_per_sec": 10.0,
    })
    assert r.status_code == 200


# ── /api/throughput/stats ────────────────────────────────────────────────────

def test_throughput_stats_returns_models(client):
    # Seed a sample first
    client.post("/api/throughput/record", json={
        "model": "stats-test-model",
        "output_tokens_per_sec": 30.0,
        "ttft_ms": 120.0,
    })
    r = client.get("/api/throughput/stats")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "models" in data
    # The seeded model should appear
    if "stats-test-model" in data["models"]:
        m = data["models"]["stats-test-model"]
        assert "latest" in m
        assert "peak" in m
        assert "p50" in m
        assert "p95" in m
        assert "sample_count" in m
        assert m["sample_count"] >= 1


# ── /api/throughput/service-usage ────────────────────────────────────────────

def test_throughput_service_usage_returns_by_model(client):
    client.post("/api/throughput/record", json={
        "model": "usage-test",
        "output_tokens_per_sec": 20.0,
        "service": "open-webui",
    })
    r = client.get("/api/throughput/service-usage")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "by_model" in data


# ── /api/auth/config ─────────────────────────────────────────────────────────

def test_auth_config_no_auth(client):
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    data = r.json()
    assert data["auth_required"] is False


# ── Global exception handler ────────────────────────────────────────────────

def test_unhandled_exception_returns_500_not_traceback(monkeypatch):
    import dashboard.app as dashboard_app

    monkeypatch.setattr(dashboard_app, "_AUTH_REQUIRED", False)
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr("dashboard.app._http_client", mock_client)

    # Patch ollama library to raise an unexpected error
    monkeypatch.setattr("dashboard.app._fetch_ollama_library", lambda: (_ for _ in ()).throw(RuntimeError("test boom")))

    tc = TestClient(dashboard_app.app, raise_server_exceptions=False)
    r = tc.get("/api/ollama/library")
    assert r.status_code == 500
    data = r.json()
    assert data["detail"] == "Internal server error"
    # Must NOT contain the traceback
    assert "test boom" not in str(data)
