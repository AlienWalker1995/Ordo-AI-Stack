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
