"""Tests for the dashboard's request authentication (dashboard/auth.py + auth_middleware).

The dashboard forwards operator actions to ops-controller with its own OPS_CONTROLLER_TOKEN,
so it must only act for a caller who could have acted directly: an operator signed in through
the Caddy SSO edge, or an internal caller that already holds OPS_CONTROLLER_TOKEN. Anything else
on the stack network (Open WebUI tools, n8n workflows, searxng) must not be able to borrow the
dashboard's credential (the confused deputy found in the 2026-09-24 architecture audit, F2).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

OPS_TOKEN = "test-ops-controller-token"
CADDY_IP = "10.9.0.7"
OTHER_CONTAINER_IP = "10.9.0.42"

DASHBOARD_DOCKERFILE = (
    Path(__file__).resolve().parents[1] / "services" / "dashboard" / "dashboard" / "Dockerfile"
)


@pytest.fixture
def ops_http(monkeypatch):
    """Stub the dashboard's shared HTTP client; `.request` is what forwards to ops-controller."""
    import dashboard.app  # noqa: F401  (load the module before patching it)

    async def _stub_check(url: str, client=None):
        return (True, "")

    monkeypatch.setattr("dashboard.services_catalog._check_service", _stub_check)

    ops_response = MagicMock(status_code=200)
    ops_response.json.return_value = {"ok": True}
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    mock_client.request = AsyncMock(return_value=ops_response)
    monkeypatch.setattr("dashboard.app._http_client", mock_client)
    return mock_client


@pytest.fixture
def auth_config(monkeypatch):
    """OPS_CONTROLLER_TOKEN configured, and `caddy` resolving to CADDY_IP."""
    import dashboard.app as dashboard_app
    import dashboard.auth as auth
    import dashboard.settings as settings

    monkeypatch.setattr(settings, "OPS_CONTROLLER_TOKEN", OPS_TOKEN)
    monkeypatch.setattr(dashboard_app, "OPS_CONTROLLER_TOKEN", OPS_TOKEN)
    monkeypatch.setattr(auth, "edge_proxy_addresses", lambda: {CADDY_IP})


def _client(peer_ip: str) -> TestClient:
    import dashboard.app as dashboard_app

    return TestClient(dashboard_app.app, client=(peer_ip, 50000))


@pytest.fixture
def from_other_container(ops_http, auth_config) -> TestClient:
    """A request from some other container on the stack network (not the edge)."""
    return _client(OTHER_CONTAINER_IP)


@pytest.fixture
def from_edge(ops_http, auth_config) -> TestClient:
    """A request whose TCP peer is the Caddy edge."""
    return _client(CADDY_IP)


# Every route that changes state or forwards to ops-controller. (method, path, json body)
PROTECTED_ROUTES = [
    ("POST", "/api/ops/services/searxng/start", None),
    ("POST", "/api/ops/services/searxng/stop", None),
    ("POST", "/api/ops/services/searxng/restart", None),
    ("GET", "/api/ops/services/searxng/logs", None),
    ("POST", "/api/models/switch", {"model": "x"}),
    ("POST", "/api/models/delete", {"file": "x.gguf"}),
    ("POST", "/api/orchestration/comfyui/restart", {"confirm": True}),
    ("GET", "/api/orchestration/comfyui/status", None),
    ("GET", "/api/orchestration/registry/models", None),
    ("GET", "/api/orchestration/registry/gpus", None),
    ("GET", "/api/orchestration/gpu/history", None),
    ("GET", "/api/orchestration/workflows", None),
    ("POST", "/api/orchestration/workflows/save", {"workflow_id": "x", "workflow": {}}),
    ("POST", "/api/mcp/add", {"server": "x"}),
    ("POST", "/api/mcp/remove", {"server": "x"}),
    ("DELETE", "/api/comfyui/models/loras/x.safetensors", None),
    ("POST", "/api/comfyui/install-node-requirements", {"confirm": True}),
    ("POST", "/api/throughput/benchmark", {}),
]


def _call(client: TestClient, method: str, path: str, body, headers=None):
    return client.request(method, path, json=body, headers=headers or {})


# --------------------------------------------------------------------------- #
# Open routes: the SPA shell, health, and read-only views stay open as before
# --------------------------------------------------------------------------- #


class TestOpenRoutes:
    def test_spa_shell_is_open(self, from_other_container):
        assert from_other_container.get("/").status_code != 401

    @pytest.mark.parametrize("path", ["/api/health", "/api/orchestration/readiness"])
    def test_health_routes_are_open(self, from_other_container, path):
        assert from_other_container.get(path).status_code != 401

    @pytest.mark.parametrize("path", ["/api/mcp/servers", "/api/models"])
    def test_read_only_views_are_open(self, from_other_container, path):
        assert from_other_container.get(path).status_code != 401


# --------------------------------------------------------------------------- #
# The confused deputy: no credential, no forwarding
# --------------------------------------------------------------------------- #


class TestUnauthenticatedCallersAreRefused:
    @pytest.mark.parametrize(("method", "path", "body"), PROTECTED_ROUTES)
    def test_protected_route_refuses_anonymous_caller(self, from_other_container, method, path, body):
        r = _call(from_other_container, method, path, body)
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}: {r.text}"

    def test_refused_call_never_reaches_ops_controller(self, from_other_container, ops_http):
        r = from_other_container.post("/api/ops/services/searxng/restart")
        assert r.status_code == 401
        ops_http.request.assert_not_awaited()

    def test_unknown_mutation_is_refused_by_default(self, from_other_container):
        """A future POST route is protected without anyone remembering to list it."""
        assert from_other_container.post("/api/some/future/action", json={}).status_code == 401

    def test_forged_edge_identity_from_another_container_is_refused(self, from_other_container):
        r = from_other_container.post(
            "/api/ops/services/searxng/restart",
            headers={"X-Forwarded-Email": "operator@example.com"},
        )
        assert r.status_code == 401

    def test_wrong_bearer_is_refused(self, from_other_container):
        r = from_other_container.post(
            "/api/ops/services/searxng/restart", headers={"Authorization": "Bearer wrong"}
        )
        assert r.status_code == 401

    def test_malformed_authorization_is_refused(self, from_other_container):
        r = from_other_container.post(
            "/api/ops/services/searxng/restart", headers={"Authorization": f"Token {OPS_TOKEN}"}
        )
        assert r.status_code == 401

    def test_empty_bearer_never_matches_an_unset_token(self, ops_http, auth_config, monkeypatch):
        import dashboard.settings as settings

        monkeypatch.setattr(settings, "OPS_CONTROLLER_TOKEN", "")
        r = _client(OTHER_CONTAINER_IP).post(
            "/api/ops/services/searxng/restart", headers={"Authorization": "Bearer "}
        )
        assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Accepted principals: the SSO edge identity, or the ops-controller bearer
# --------------------------------------------------------------------------- #


class TestEdgeIdentity:
    def test_edge_identity_from_caddy_is_accepted(self, from_edge, ops_http):
        r = from_edge.post(
            "/api/ops/services/searxng/restart",
            headers={"X-Forwarded-Email": "operator@example.com"},
        )
        assert r.status_code == 200, r.text
        ops_http.request.assert_awaited_once()

    def test_caddy_without_identity_is_refused(self, from_edge):
        """Fail closed: a request through the edge that carries no identity is anonymous."""
        assert from_edge.post("/api/ops/services/searxng/restart").status_code == 401

    def test_caddy_with_empty_identity_is_refused(self, from_edge):
        r = from_edge.post("/api/ops/services/searxng/restart", headers={"X-Forwarded-Email": " "})
        assert r.status_code == 401

    def test_unresolvable_edge_fails_closed(self, ops_http, auth_config, monkeypatch):
        import dashboard.auth as auth

        monkeypatch.setattr(auth, "edge_proxy_addresses", lambda: set())
        r = _client(CADDY_IP).post(
            "/api/ops/services/searxng/restart",
            headers={"X-Forwarded-Email": "operator@example.com"},
        )
        assert r.status_code == 401


class TestOpsControllerBearer:
    @pytest.mark.parametrize(("method", "path", "body"), PROTECTED_ROUTES)
    def test_bearer_passes_the_auth_gate(self, from_other_container, method, path, body):
        r = _call(from_other_container, method, path, body, {"Authorization": f"Bearer {OPS_TOKEN}"})
        assert r.status_code != 401, f"{method} {path} -> 401: {r.text}"

    def test_bearer_call_is_forwarded(self, from_other_container, ops_http):
        r = from_other_container.post(
            "/api/ops/services/searxng/restart", headers={"Authorization": f"Bearer {OPS_TOKEN}"}
        )
        assert r.status_code == 200, r.text
        ops_http.request.assert_awaited_once()


# --------------------------------------------------------------------------- #
# /api/throughput/record keeps its own X-Throughput-Token (model-gateway callback)
# --------------------------------------------------------------------------- #


class TestThroughputRecordAuth:
    def test_throughput_record_rejects_missing_token(self, from_other_container, monkeypatch):
        monkeypatch.setenv("THROUGHPUT_RECORD_TOKEN", "tp-secret")
        r = from_other_container.post("/api/throughput/record", json={})
        assert r.status_code == 401
        assert "X-Throughput-Token" in r.json()["detail"]

    def test_throughput_record_rejects_wrong_token(self, from_other_container, monkeypatch):
        monkeypatch.setenv("THROUGHPUT_RECORD_TOKEN", "tp-secret")
        r = from_other_container.post(
            "/api/throughput/record", json={}, headers={"X-Throughput-Token": "wrong"}
        )
        assert r.status_code == 401

    def test_throughput_record_accepts_correct_token(self, from_other_container, monkeypatch):
        monkeypatch.setenv("THROUGHPUT_RECORD_TOKEN", "tp-secret")
        r = from_other_container.post(
            "/api/throughput/record", json={}, headers={"X-Throughput-Token": "tp-secret"}
        )
        assert r.status_code != 401


# --------------------------------------------------------------------------- #
# The peer address the edge check reads must be the real TCP peer
# --------------------------------------------------------------------------- #


def test_uvicorn_never_rewrites_the_peer_from_forwarded_headers():
    """With proxy headers on, uvicorn replaces request.client with X-Forwarded-For whenever the
    peer is in FORWARDED_ALLOW_IPS (the shared .env could set it). A forged X-Forwarded-For would
    then pass the edge-peer check, so the dashboard pins --no-proxy-headers."""
    text = DASHBOARD_DOCKERFILE.read_text(encoding="utf-8")
    cmd = next(line for line in text.splitlines() if line.startswith("CMD"))
    assert "--no-proxy-headers" in cmd
