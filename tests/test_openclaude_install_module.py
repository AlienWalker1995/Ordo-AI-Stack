"""Unit tests for dashboard.openclaude_install."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from dashboard.openclaude_install import (
    HostnameResolutionError,
    resolve_tailnet_hostname,
)


def test_ts_hostname_env_takes_precedence(monkeypatch):
    monkeypatch.setenv("TS_HOSTNAME", "explicit.example.ts.net")
    assert resolve_tailnet_hostname(host_header="other.example.ts.net:8080") == "explicit.example.ts.net"


def test_falls_back_to_host_header_minus_port(monkeypatch):
    monkeypatch.delenv("TS_HOSTNAME", raising=False)
    assert resolve_tailnet_hostname(host_header="my-host.tail1234.ts.net:8080") == "my-host.tail1234.ts.net"


def test_host_header_without_port(monkeypatch):
    monkeypatch.delenv("TS_HOSTNAME", raising=False)
    assert resolve_tailnet_hostname(host_header="my-host.tail1234.ts.net") == "my-host.tail1234.ts.net"


def test_localhost_host_header_raises(monkeypatch):
    monkeypatch.delenv("TS_HOSTNAME", raising=False)
    with pytest.raises(HostnameResolutionError):
        resolve_tailnet_hostname(host_header="localhost:8080")


def test_loopback_ip_host_header_raises(monkeypatch):
    monkeypatch.delenv("TS_HOSTNAME", raising=False)
    with pytest.raises(HostnameResolutionError):
        resolve_tailnet_hostname(host_header="127.0.0.1:8080")


def test_no_inputs_raises(monkeypatch):
    monkeypatch.delenv("TS_HOSTNAME", raising=False)
    with pytest.raises(HostnameResolutionError):
        resolve_tailnet_hostname(host_header=None)


from unittest.mock import AsyncMock, MagicMock

from dashboard.openclaude_install import BlogMcpPreflight


@pytest.mark.asyncio
async def test_blog_preflight_returns_true_on_2xx():
    client = MagicMock()
    response = MagicMock(status_code=200)
    client.get = AsyncMock(return_value=response)

    preflight = BlogMcpPreflight(url="http://host.docker.internal:3500/mcp", ttl_seconds=10)
    assert await preflight.is_reachable(client) is True
    client.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_blog_preflight_returns_false_on_connection_error():
    import httpx

    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.RequestError("connection refused", request=MagicMock()))

    preflight = BlogMcpPreflight(url="http://host.docker.internal:3500/mcp", ttl_seconds=10)
    assert await preflight.is_reachable(client) is False


@pytest.mark.asyncio
async def test_blog_preflight_caches_result_within_ttl():
    client = MagicMock()
    client.get = AsyncMock(return_value=MagicMock(status_code=200))

    preflight = BlogMcpPreflight(url="http://x/mcp", ttl_seconds=60)
    await preflight.is_reachable(client)
    await preflight.is_reachable(client)
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_blog_preflight_refreshes_after_ttl():
    client = MagicMock()
    client.get = AsyncMock(return_value=MagicMock(status_code=200))

    preflight = BlogMcpPreflight(url="http://x/mcp", ttl_seconds=0)
    await preflight.is_reachable(client)
    await preflight.is_reachable(client)
    assert client.get.await_count == 2


import json

from dashboard.openclaude_install import (
    render_claude_json,
    render_install_script_ps1,
    render_install_script_sh,
)


def test_render_claude_json_includes_gateway_and_local_tools_only_when_blog_unreachable():
    body = render_claude_json(
        host="my-host.tail.ts.net",
        mcp_gateway_port=8811,
        blog_reachable=False,
        blog_port=3500,
        blog_api_key="",
        local_workspace_path="/Users/me/openclaude-workspace",
    )
    parsed = json.loads(body)
    assert "gateway" in parsed["mcpServers"]
    assert "local-tools" in parsed["mcpServers"]
    assert "blog" not in parsed["mcpServers"]
    assert parsed["mcpServers"]["gateway"]["url"] == "http://my-host.tail.ts.net:8811/mcp"
    assert parsed["mcpServers"]["gateway"]["transport"] == "http"
    assert parsed["mcpServers"]["local-tools"]["transport"] == "stdio"
    assert "/Users/me/openclaude-workspace" in parsed["mcpServers"]["local-tools"]["args"]


def test_render_claude_json_includes_blog_when_reachable():
    body = render_claude_json(
        host="my-host.tail.ts.net",
        mcp_gateway_port=8811,
        blog_reachable=True,
        blog_port=3500,
        blog_api_key="secret-key-123",
        local_workspace_path="/home/me/openclaude-workspace",
    )
    parsed = json.loads(body)
    assert parsed["mcpServers"]["blog"]["url"] == "http://my-host.tail.ts.net:3500/mcp"
    assert parsed["mcpServers"]["blog"]["headers"] == {"x-api-key": "secret-key-123"}


def test_render_claude_json_omits_blog_when_no_api_key_even_if_reachable():
    body = render_claude_json(
        host="x", mcp_gateway_port=8811,
        blog_reachable=True, blog_port=3500, blog_api_key="",
        local_workspace_path="/x",
    )
    parsed = json.loads(body)
    assert "blog" not in parsed["mcpServers"]


def _render_kwargs(blog_reachable=False):
    return dict(
        host="my-host.tail.ts.net",
        model_gateway_port=11435,
        mcp_gateway_port=8811,
        master_key="local",
        blog_reachable=blog_reachable,
        blog_port=3500,
        blog_api_key="key" if blog_reachable else "",
    )


def test_sh_script_contains_required_actions():
    script = render_install_script_sh(**_render_kwargs())
    assert script.startswith("#!/usr/bin/env sh")
    assert "command -v node" in script
    assert "command -v rg" in script
    assert "npm install -g @gitlawb/openclaude" in script
    assert 'OPENAI_BASE_URL=' in script
    assert 'OPENAI_API_KEY=' in script
    assert 'openclaude --model local-chat' in script


def test_sh_script_writes_claude_json_with_local_tools():
    script = render_install_script_sh(**_render_kwargs())
    assert "$HOME/openclaude-workspace" in script
    assert "mkdir -p" in script


def test_ps1_script_contains_required_actions():
    script = render_install_script_ps1(**_render_kwargs())
    assert "Get-Command node" in script
    assert "Get-Command rg" in script
    assert "npm install -g" in script
    assert "openclaude --model local-chat" in script


def test_sh_script_omits_blog_block_when_unreachable():
    script = render_install_script_sh(**_render_kwargs(blog_reachable=False))
    assert "BLOG_MCP=" not in script


def test_sh_script_includes_blog_block_when_reachable():
    script = render_install_script_sh(**_render_kwargs(blog_reachable=True))
    assert "BLOG_MCP=" in script
    assert "BLOG_API_KEY=" in script
