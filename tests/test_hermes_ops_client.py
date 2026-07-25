"""Tests for the Hermes OpsClient — the ops-api HTTP wrapper.

Moved here from the sibling copy (audit P1-8): the pytest CI job
only collects tests/, so the sibling copy never ran. The module is loaded
by file path (importlib spec_from_file_location) rather than
``from services.hermes.ops_client import ...`` so collection doesn't depend
on the build context being importable as a package (it has no __init__.py).

Also updated for the fixed wiring (audit P0-2): OpsClient now talks to
**ops-api** (OPS_API_URL, default http://ops-api:9000), not the
ops-controller scheduler which 404s on every container/compose route.
Compose verbs map to ops-api's per-service surface — POST
/services/{name}/recreate (up/restart) and POST /services/{name}/stop
(down) — since ops-api's stack-wide /compose/* is a deliberate 501.

respx mocks the HTTPX transport so no network or live ops-api is required.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import respx
from httpx import Response

ROOT = Path(__file__).resolve().parents[1]
OPS_CLIENT_PY = ROOT / "services" / "hermes" / "ops_client.py"

_spec = importlib.util.spec_from_file_location("hermes_ops_client_under_test", OPS_CLIENT_PY)
ops_client_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ops_client_mod)
OpsClient = ops_client_mod.OpsClient
OpsClientError = ops_client_mod.OpsClientError

BASE_URL = "http://ops-api:9000"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "test-token")
    monkeypatch.setenv("OPS_API_URL", BASE_URL)
    return OpsClient()


def test_default_base_url_is_ops_api_not_ops_controller(monkeypatch):
    """No OPS_API_URL set -> defaults to http://ops-api:9000. Setting
    OPS_CONTROLLER_URL (the scheduler that 404s on these routes) must have
    no effect — that was the original mis-wiring (audit P0-2)."""
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "test-token")
    monkeypatch.delenv("OPS_API_URL", raising=False)
    monkeypatch.setenv("OPS_CONTROLLER_URL", "http://ops-controller:9000")
    c = OpsClient()
    assert c.url == BASE_URL


def test_token_required(monkeypatch):
    monkeypatch.delenv("OPS_CONTROLLER_TOKEN", raising=False)
    monkeypatch.setenv("OPS_API_URL", BASE_URL)
    with pytest.raises(OpsClientError):
        OpsClient()


def test_list_containers_includes_bearer(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/containers").mock(
            return_value=Response(200, json=[{"name": "a", "status": "running", "image": "x"}])
        )
        out = client.list_containers()
        request = mock.calls.last.request
        assert request.headers["Authorization"] == "Bearer test-token"
    assert out[0]["name"] == "a"


def test_restart_unknown_raises_ops_client_error(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/containers/missing/restart").mock(
            return_value=Response(404, json={"detail": "not found"})
        )
        with pytest.raises(OpsClientError) as ei:
            client.restart_container("missing")
        assert "not found" in str(ei.value).lower()


def test_logs_returns_string(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/containers/foo/logs").mock(return_value=Response(200, text="line1\nline2"))
        assert client.container_logs("foo") == "line1\nline2"


# ── compose verbs: per-service recreate/stop; stack-wide refused client-side ──


def test_compose_up_recreates_named_service(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/services/comfyui/recreate").mock(
            return_value=Response(200, json={"ok": True, "service": "comfyui"})
        )
        out = client.compose_up(service="comfyui", confirm=True)
        body = mock.calls.last.request.read()
        assert b'"confirm":true' in body or b'"confirm": true' in body
        assert out["service"] == "comfyui"


def test_compose_restart_maps_to_recreate(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/services/agent/recreate").mock(return_value=Response(200, json={"ok": True}))
        client.compose_restart(service="agent", confirm=True)
        assert mock.calls.last.request.url.path == "/services/agent/recreate"


def test_compose_down_maps_to_stop(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/services/agent/stop").mock(return_value=Response(200, json={"ok": True}))
        client.compose_down(service="agent")
        assert mock.calls.last.request.url.path == "/services/agent/stop"


def test_compose_verbs_without_service_raise(client):
    """Stack-wide /compose/* is a deliberate 501 on ops-api; OpsClient refuses
    client-side rather than hitting a route that always fails."""
    with pytest.raises(OpsClientError, match="stack-wide"):
        client.compose_restart(service=None)
    with pytest.raises(OpsClientError, match="stack-wide"):
        client.compose_up(service=None)
    with pytest.raises(OpsClientError, match="stack-wide"):
        client.compose_down(service=None)
