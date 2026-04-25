"""Tests for hermes.ops_client.OpsClient.

These exercise the HTTP wrapper Hermes uses to reach the ops-controller
service over the Docker network. respx mocks the HTTPX transport so no
network or live ops-controller is required.
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from hermes.ops_client import OpsClient, OpsClientError


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "test-token")
    monkeypatch.setenv("OPS_CONTROLLER_URL", "http://ops-controller:9000")
    return OpsClient()


def test_list_containers_includes_bearer(client):
    with respx.mock(base_url="http://ops-controller:9000") as mock:
        mock.get("/containers").mock(
            return_value=Response(200, json=[{"name": "a", "status": "running", "image": "x"}])
        )
        out = client.list_containers()
        request = mock.calls.last.request
        assert request.headers["Authorization"] == "Bearer test-token"
    assert out[0]["name"] == "a"


def test_restart_unknown_raises_ops_client_error(client):
    with respx.mock(base_url="http://ops-controller:9000") as mock:
        mock.post("/containers/missing/restart").mock(
            return_value=Response(404, json={"detail": "not found"})
        )
        with pytest.raises(OpsClientError) as ei:
            client.restart_container("missing")
        assert "not found" in str(ei.value).lower()


def test_compose_restart_whole_stack_requires_confirm(client):
    with respx.mock(base_url="http://ops-controller:9000") as mock:
        mock.post("/compose/restart").mock(
            return_value=Response(200, json={"verb": "restart", "target": "all"})
        )
        client.compose_restart(service=None, confirm=True)
        body = mock.calls.last.request.read()
        assert b'"confirm":true' in body or b'"confirm": true' in body


def test_logs_returns_string(client):
    with respx.mock(base_url="http://ops-controller:9000") as mock:
        mock.get("/containers/foo/logs").mock(return_value=Response(200, text="line1\nline2"))
        assert client.container_logs("foo") == "line1\nline2"


def test_token_required(monkeypatch):
    monkeypatch.delenv("OPS_CONTROLLER_TOKEN", raising=False)
    monkeypatch.setenv("OPS_CONTROLLER_URL", "http://ops-controller:9000")
    with pytest.raises(OpsClientError):
        OpsClient()
