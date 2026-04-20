"""MCP policy and API contract tests.

Tests response structure of /api/mcp/servers and /api/mcp/health.
Registry allow_clients is for future gateway enforcement; these tests assert API shape.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient

from dashboard.app import app

client = TestClient(app)


def test_mcp_servers_returns_enabled_catalog_dynamic_registry():
    """GET /api/mcp/servers returns enabled, catalog, dynamic, registry, ok."""
    with patch("dashboard.app._read_mcp_servers", return_value=["duckduckgo", "fetch"]), \
         patch("dashboard.app._mcp_config_path", return_value=__import__("pathlib").Path("/tmp/servers.txt")), \
         patch("dashboard.app._read_mcp_registry", return_value={
             "servers": {"duckduckgo": {"allow_clients": ["*"]}, "fetch": {"allow_clients": ["*"]}}
         }):
        r = client.get("/api/mcp/servers")
    assert r.status_code == 200
    data = r.json()
    assert "enabled" in data
    assert isinstance(data["enabled"], list)
    assert "catalog" in data
    assert isinstance(data["catalog"], list)
    assert "dynamic" in data
    assert "registry" in data
    assert "servers" in data["registry"]
    assert data["ok"] is True


def test_mcp_health_returns_ok_gateway_servers_list():
    """GET /api/mcp/health returns ok, gateway, gateway_error, servers (list with id/ok/status)."""
    from unittest.mock import AsyncMock, MagicMock
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    with patch("dashboard.app._read_mcp_servers", return_value=["duckduckgo"]), \
         patch("dashboard.app.OPS_CONTROLLER_TOKEN", ""), \
         patch("httpx.AsyncClient") as m:
        inst = MagicMock()
        inst.get = AsyncMock(return_value=mock_resp)
        m.return_value.__aenter__ = AsyncMock(return_value=inst)
        m.return_value.__aexit__ = AsyncMock(return_value=None)
        r = client.get("/api/mcp/health")
    assert r.status_code == 200
    data = r.json()
    assert "ok" in data
    assert "gateway" in data
    assert "servers" in data
    assert isinstance(data["servers"], list)
    for s in data["servers"]:
        assert "id" in s
        assert "ok" in s
        assert "status" in s or "error" in s


def test_mcp_servers_registry_allow_clients_structure():
    """Registry servers may have allow_clients; API returns registry for future policy use."""
    with patch("dashboard.app._read_mcp_servers", return_value=["duckduckgo"]), \
         patch("dashboard.app._mcp_config_path", return_value=__import__("pathlib").Path("/tmp/servers.txt")), \
         patch("dashboard.app._read_mcp_registry", return_value={
             "servers": {
                 "duckduckgo": {"allow_clients": ["*"], "scopes": ["search"]},
                 "github-official": {"allow_clients": ["dashboard"], "env_schema": {"GITHUB_PERSONAL_ACCESS_TOKEN": "required"}},
             }
         }):
        r = client.get("/api/mcp/servers")
    assert r.status_code == 200
    reg = r.json().get("registry", {})
    assert "duckduckgo" in reg.get("servers", {})
    assert reg["servers"]["duckduckgo"].get("allow_clients") == ["*"]
    assert reg["servers"]["github-official"].get("allow_clients") == ["dashboard"]
