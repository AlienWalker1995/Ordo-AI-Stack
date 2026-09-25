"""Tests for the Hermes OpsClient — the control plane's HTTP wrapper.

Moved here from the sibling copy (audit P1-8): the pytest CI job
only collects tests/, so the sibling copy never ran. The module is loaded
by file path (importlib spec_from_file_location) rather than
``from services.hermes.ops_client import ...`` so collection doesn't depend
on the build context being importable as a package (it has no __init__.py).

These verbs lived on a separate Bearer-gated `ops-api` while they were being
ported off it (audit P0-2 was the mis-wiring in the other direction: the client
pointed at a scheduler that 404'd them). ops-controller serves all of them now,
so there is ONE base URL: OPS_CONTROLLER_URL, default http://ops-controller:9000.
Compose verbs still map to the per-service surface — POST
/services/{name}/recreate (up/restart) and POST /services/{name}/stop (down) —
since the stack-wide /compose/* is a deliberate 501.

respx mocks the HTTPX transport so no network or live control plane is required.
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

BASE_URL = "http://ops-controller:9000"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "test-token")
    monkeypatch.setenv("OPS_API_URL", BASE_URL)
    return OpsClient()


def test_base_url_defaults_to_the_control_plane(monkeypatch):
    """No env set -> http://ops-controller:9000, the one service that serves these routes."""
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "test-token")
    monkeypatch.delenv("OPS_CONTROLLER_URL", raising=False)
    assert OpsClient().url == BASE_URL


def test_base_url_follows_ops_controller_url(monkeypatch):
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "test-token")
    monkeypatch.setenv("OPS_CONTROLLER_URL", "http://elsewhere:9000")
    c = OpsClient()
    assert c.url == "http://elsewhere:9000"
    assert c.ctl_url == "http://elsewhere:9000"  # one service, so both point at it


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
        assert request.headers["X-Actor"] == "hermes"   # the caller ops-controller's audit log records
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
    """Stack-wide /compose/* is a deliberate 501; OpsClient refuses
    client-side rather than hitting a route that always fails."""
    with pytest.raises(OpsClientError, match="stack-wide"):
        client.compose_restart(service=None)
    with pytest.raises(OpsClientError, match="stack-wide"):
        client.compose_up(service=None)
    with pytest.raises(OpsClientError, match="stack-wide"):
        client.compose_down(service=None)


def test_compose_down_sends_confirm(client):
    """compose_down used to post to /services/{id}/stop with no body at all, so
    ops-controller's `if not body.get("confirm")` check 400'd every call. It must send
    confirm the same way compose_up/compose_restart already do."""
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/services/agent/stop").mock(return_value=Response(200, json={"ok": True}))
        client.compose_down(service="agent", confirm=True)
        body = mock.calls.last.request.read()
        assert b'"confirm":true' in body or b'"confirm": true' in body


# ── every public method must send the bearer + X-Actor (audit trail), not just the ones
# that happen to use the client with headers on it ──


def _cover_every_public_method(client, mock):
    """Registers a 200 mock for every ops-controller route this client calls, then invokes
    every public OpsClient method once. Returns the respx mock so callers can inspect calls."""
    mock.get("/containers").mock(return_value=Response(200, json=[]))
    mock.get("/containers/foo/logs").mock(return_value=Response(200, text="ok"))
    mock.post("/containers/foo/restart").mock(return_value=Response(200, json={"ok": True}))
    mock.post("/services/foo/recreate").mock(return_value=Response(200, json={"ok": True}))
    mock.post("/services/foo/stop").mock(return_value=Response(200, json={"ok": True}))
    mock.get("/plugins").mock(return_value=Response(200, json={"plugins": []}))
    mock.post("/plugins/foo/enable").mock(return_value=Response(200, json={"ok": True}))
    mock.post("/plugins/foo/disable").mock(return_value=Response(200, json={"ok": True}))

    client.list_containers()
    client.container_logs("foo")
    client.restart_container("foo")
    client.compose_up(service="foo", confirm=True)
    client.compose_restart(service="foo", confirm=True)
    client.compose_down(service="foo", confirm=True)
    client.list_plugins()
    client.enable_plugin("foo", confirm=True)
    client.disable_plugin("foo", confirm=True)


def test_every_public_method_sends_the_bearer_and_actor(client):
    """The plugin enable/disable/list methods used a second httpx.Client built with no
    headers at all ('ControlPlane is authless by design' — stale: ops-controller requires
    a bearer on every path but /health, see ordo/control/api.py `UNAUTHENTICATED_PATHS`), so
    they 401'd on every call. Every public method must go through the same authenticated
    request path."""
    with respx.mock(base_url=BASE_URL) as mock:
        _cover_every_public_method(client, mock)
        assert mock.calls, "no requests were made"
        for call in mock.calls:
            assert call.request.headers["Authorization"] == "Bearer test-token", (
                f"{call.request.method} {call.request.url.path} sent no bearer token"
            )
            assert call.request.headers["X-Actor"] == "hermes", (
                f"{call.request.method} {call.request.url.path} sent no X-Actor header"
            )
