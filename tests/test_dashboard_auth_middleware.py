"""Tests for dashboard auth_middleware (dashboard/app.py)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def _stub_services(monkeypatch):
    """Stub service probes so TestClient doesn't hit real Docker DNS."""
    from unittest.mock import AsyncMock, MagicMock

    import dashboard.app  # noqa: F401 — ensure module is loaded before patching

    async def _stub_check(url: str, client=None):
        return (True, "")

    monkeypatch.setattr("dashboard.services_catalog._check_service", _stub_check)

    # Stub shared HTTP client so endpoints using _get_http_client() don't fail
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr("dashboard.app._http_client", mock_client)


@pytest.fixture
def client_no_auth(_stub_services, monkeypatch):
    """Client where DASHBOARD_AUTH_TOKEN is empty (auth not required)."""
    import dashboard.app as dashboard_app

    monkeypatch.setattr(dashboard_app, "_AUTH_REQUIRED", False)
    monkeypatch.setattr(dashboard_app, "DASHBOARD_AUTH_TOKEN", "")
    return TestClient(dashboard_app.app)


@pytest.fixture
def client_with_auth(_stub_services, monkeypatch):
    """Client where DASHBOARD_AUTH_TOKEN is set (auth required)."""
    import dashboard.app as dashboard_app

    monkeypatch.setattr(dashboard_app, "_AUTH_REQUIRED", True)
    monkeypatch.setattr(dashboard_app, "DASHBOARD_AUTH_TOKEN", "test-secret-token")
    return TestClient(dashboard_app.app)


# --------------------------------------------------------------------------- #
# 1. Non-/api/ paths bypass auth entirely
# --------------------------------------------------------------------------- #


class TestNonApiPathsBypassAuth:
    """Requests to non-/api/ paths should never be blocked by auth."""

    def test_root_returns_success_without_auth(self, client_with_auth):
        r = client_with_auth.get("/")
        # Static file or redirect — either way, not 401
        assert r.status_code != 401

    def test_static_asset_returns_success_without_auth(self, client_with_auth):
        r = client_with_auth.get("/index.html")
        assert r.status_code != 401


# --------------------------------------------------------------------------- #
# 2. Allowlisted /api/ paths bypass auth
# --------------------------------------------------------------------------- #


ALLOWLISTED_PATHS = [
    "/api/health",
    "/api/dependencies",
    "/api/auth/config",
    "/api/hardware",
    "/api/rag/status",
    "/api/orchestration/readiness",
]


class TestAllowlistedPathsBypassAuth:
    """Allowlisted API endpoints should be accessible without a Bearer token."""

    @pytest.mark.parametrize("path", ALLOWLISTED_PATHS)
    def test_allowlisted_path_no_401(self, client_with_auth, path):
        r = client_with_auth.get(path)
        assert r.status_code != 401, f"{path} should bypass auth but got 401"


# --------------------------------------------------------------------------- #
# 3. /api/throughput/record uses X-Throughput-Token
# --------------------------------------------------------------------------- #


class TestThroughputRecordAuth:
    """The /api/throughput/record endpoint uses its own X-Throughput-Token header."""

    def test_throughput_record_rejects_missing_token(self, client_no_auth, monkeypatch):
        monkeypatch.setenv("THROUGHPUT_RECORD_TOKEN", "tp-secret")
        r = client_no_auth.post("/api/throughput/record", json={})
        assert r.status_code == 401
        assert "X-Throughput-Token" in r.json()["detail"]

    def test_throughput_record_rejects_wrong_token(self, client_no_auth, monkeypatch):
        monkeypatch.setenv("THROUGHPUT_RECORD_TOKEN", "tp-secret")
        r = client_no_auth.post(
            "/api/throughput/record",
            json={},
            headers={"X-Throughput-Token": "wrong"},
        )
        assert r.status_code == 401

    def test_throughput_record_accepts_correct_token(self, client_no_auth, monkeypatch):
        monkeypatch.setenv("THROUGHPUT_RECORD_TOKEN", "tp-secret")
        r = client_no_auth.post(
            "/api/throughput/record",
            json={},
            headers={"X-Throughput-Token": "tp-secret"},
        )
        # Should pass auth — may be 422 (bad body) or 200 but not 401
        assert r.status_code != 401

    def test_throughput_record_no_token_configured_allows_request(self, client_no_auth, monkeypatch):
        monkeypatch.setenv("THROUGHPUT_RECORD_TOKEN", "")
        r = client_no_auth.post("/api/throughput/record", json={})
        assert r.status_code != 401


# --------------------------------------------------------------------------- #
# 4. Other /api/* paths require Bearer token when AUTH_REQUIRED
# --------------------------------------------------------------------------- #


class TestProtectedApiEndpoints:
    """Other /api/* endpoints must require Bearer token when DASHBOARD_AUTH_TOKEN is set."""

    def test_protected_endpoint_rejects_no_token(self, client_with_auth):
        r = client_with_auth.get("/api/models")
        assert r.status_code == 401

    def test_protected_endpoint_rejects_wrong_token(self, client_with_auth):
        r = client_with_auth.get(
            "/api/models",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert r.status_code == 401

    def test_protected_endpoint_rejects_malformed_header(self, client_with_auth):
        r = client_with_auth.get(
            "/api/models",
            headers={"Authorization": "Token test-secret-token"},
        )
        assert r.status_code == 401

    def test_protected_endpoint_accepts_correct_token(self, client_with_auth):
        r = client_with_auth.get(
            "/api/models",
            headers={"Authorization": "Bearer test-secret-token"},
        )
        # Should pass auth — response may vary but must not be 401
        assert r.status_code != 401


# --------------------------------------------------------------------------- #
# 5. When DASHBOARD_AUTH_TOKEN is empty, auth is not required
# --------------------------------------------------------------------------- #


class TestAuthDisabled:
    """When DASHBOARD_AUTH_TOKEN is empty, all /api/* endpoints should be accessible."""

    def test_protected_endpoint_accessible_without_token(self, client_no_auth):
        r = client_no_auth.get("/api/models")
        assert r.status_code != 401

    def test_protected_endpoint_accessible_with_arbitrary_token(self, client_no_auth):
        r = client_no_auth.get(
            "/api/models",
            headers={"Authorization": "Bearer anything"},
        )
        assert r.status_code != 401
