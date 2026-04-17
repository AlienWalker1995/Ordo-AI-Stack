"""Integration tests for dashboard openclaude install routes."""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TS_HOSTNAME", "host.tailtest.ts.net")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "local")
    monkeypatch.setenv("BLOG_MCP_API_KEY", "")

    import dashboard.app as dashboard_app

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=500))
    monkeypatch.setattr("dashboard.app._http_client", mock_client)

    async def _stub_check(url: str, client=None):
        return (True, "")
    monkeypatch.setattr("dashboard.services_catalog._check_service", _stub_check)

    return TestClient(dashboard_app.app)


def test_preview_returns_200_and_expected_keys(client):
    r = client.get("/api/openclaude/preview")
    assert r.status_code == 200
    data = r.json()
    for key in ("host", "model_gateway_url", "mcp_gateway_url", "blog_mcp_reachable",
                "model", "one_liner_ps1", "one_liner_sh"):
        assert key in data, f"missing key {key}"
    assert data["host"] == "host.tailtest.ts.net"
    assert data["model"] == "local-chat"
    assert data["model_gateway_url"] == "http://host.tailtest.ts.net:11435/v1"
    assert data["mcp_gateway_url"] == "http://host.tailtest.ts.net:8811/mcp"
    assert data["one_liner_sh"].startswith("curl -fsSL http://host.tailtest.ts.net:8080/install/openclaude.sh")
    assert data["one_liner_ps1"].startswith("irm http://host.tailtest.ts.net:8080/install/openclaude.ps1")


def test_preview_503_when_no_hostname(monkeypatch):
    monkeypatch.delenv("TS_HOSTNAME", raising=False)
    import dashboard.app as dashboard_app

    mock_client = MagicMock()
    monkeypatch.setattr("dashboard.app._http_client", mock_client)

    async def _stub_check(url: str, client=None):
        return (True, "")
    monkeypatch.setattr("dashboard.services_catalog._check_service", _stub_check)

    c = TestClient(dashboard_app.app)
    r = c.get("/api/openclaude/preview", headers={"Host": "localhost:8080"})
    assert r.status_code == 503
    assert "TS_HOSTNAME" in r.json().get("detail", "")


def test_install_sh_returns_text_plain_with_substituted_host(client):
    r = client.get("/install/openclaude.sh")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["cache-control"] == "no-store"
    body = r.text
    assert body.startswith("#!/usr/bin/env sh")
    assert "host.tailtest.ts.net" in body
    assert "openclaude --model local-chat" in body


def test_install_ps1_returns_text_plain_with_substituted_host(client):
    r = client.get("/install/openclaude.ps1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "host.tailtest.ts.net" in body
    assert "openclaude --model local-chat" in body


def test_install_sh_omits_blog_when_blog_unreachable(client):
    r = client.get("/install/openclaude.sh")
    assert "BLOG_MCP=" not in r.text


def test_install_503_when_no_hostname(monkeypatch):
    monkeypatch.delenv("TS_HOSTNAME", raising=False)
    import dashboard.app as dashboard_app

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=500))
    monkeypatch.setattr("dashboard.app._http_client", mock_client)

    async def _stub_check(url: str, client=None):
        return (True, "")
    monkeypatch.setattr("dashboard.services_catalog._check_service", _stub_check)

    c = TestClient(dashboard_app.app)
    r = c.get("/install/openclaude.sh", headers={"Host": "localhost:8080"})
    assert r.status_code == 503
