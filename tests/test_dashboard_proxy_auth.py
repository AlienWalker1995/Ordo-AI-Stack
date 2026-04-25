"""Tests for trusted-proxy header auth in dashboard/_verify_auth (Plan A Task 9).

When DASHBOARD_TRUST_PROXY_HEADERS is enabled and the request originates from
DASHBOARD_TRUSTED_PROXY_NET, the X-Forwarded-Email header is honored as the
authenticated identity. Bearer-token auth still works when the proxy branch
does not apply.
"""
from __future__ import annotations

import ipaddress
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def _stub_services(monkeypatch):
    """Stub service probes so TestClient doesn't hit real Docker DNS."""
    import dashboard.app  # noqa: F401 — ensure module is loaded before patching

    from unittest.mock import AsyncMock, MagicMock

    async def _stub_check(url: str, client=None):
        return (True, "")

    monkeypatch.setattr("dashboard.services_catalog._check_service", _stub_check)

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    monkeypatch.setattr("dashboard.app._http_client", mock_client)


@pytest.fixture
def client_proxy_trusted(_stub_services, monkeypatch):
    """Auth required, trust-proxy enabled, 127.0.0.0/8 trusted (matches TestClient)."""
    import dashboard.app as dashboard_app
    import dashboard.settings as settings

    monkeypatch.setattr(dashboard_app, "_AUTH_REQUIRED", True)
    monkeypatch.setattr(dashboard_app, "DASHBOARD_AUTH_TOKEN", "test-secret-token")
    monkeypatch.setattr(settings, "AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DASHBOARD_AUTH_TOKEN", "test-secret-token")
    monkeypatch.setattr(settings, "DASHBOARD_TRUST_PROXY_HEADERS", True)
    monkeypatch.setattr(
        settings,
        "DASHBOARD_TRUSTED_PROXY_NET",
        ipaddress.ip_network("127.0.0.0/8", strict=False),
    )
    # Pin the source IP to 127.0.0.1 so the trust-proxy net check matches.
    return TestClient(dashboard_app.app, client=("127.0.0.1", 50000))


@pytest.fixture
def client_proxy_disabled(_stub_services, monkeypatch):
    """Auth required, trust-proxy disabled (default) — bearer-only mode."""
    import dashboard.app as dashboard_app
    import dashboard.settings as settings

    monkeypatch.setattr(dashboard_app, "_AUTH_REQUIRED", True)
    monkeypatch.setattr(dashboard_app, "DASHBOARD_AUTH_TOKEN", "test-secret-token")
    monkeypatch.setattr(settings, "AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DASHBOARD_AUTH_TOKEN", "test-secret-token")
    monkeypatch.setattr(settings, "DASHBOARD_TRUST_PROXY_HEADERS", False)
    monkeypatch.setattr(settings, "DASHBOARD_TRUSTED_PROXY_NET", None)
    return TestClient(dashboard_app.app)


@pytest.fixture
def client_proxy_untrusted_net(_stub_services, monkeypatch):
    """Auth required, trust-proxy enabled, but 10.0.0.0/8 trusted — TestClient (127.0.0.1) is NOT in it."""
    import dashboard.app as dashboard_app
    import dashboard.settings as settings

    monkeypatch.setattr(dashboard_app, "_AUTH_REQUIRED", True)
    monkeypatch.setattr(dashboard_app, "DASHBOARD_AUTH_TOKEN", "test-secret-token")
    monkeypatch.setattr(settings, "AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DASHBOARD_AUTH_TOKEN", "test-secret-token")
    monkeypatch.setattr(settings, "DASHBOARD_TRUST_PROXY_HEADERS", True)
    monkeypatch.setattr(
        settings,
        "DASHBOARD_TRUSTED_PROXY_NET",
        ipaddress.ip_network("10.0.0.0/8", strict=False),
    )
    # Source IP 127.0.0.1 is NOT in 10.0.0.0/8 — proxy header must be ignored.
    return TestClient(dashboard_app.app, client=("127.0.0.1", 50000))


# Protected endpoint that doesn't reach external services in tests.
PROTECTED_PATH = "/api/mcp/servers"


def test_request_from_trusted_proxy_with_email_passes(client_proxy_trusted):
    """X-Forwarded-Email from trusted proxy net authenticates the request."""
    r = client_proxy_trusted.get(
        PROTECTED_PATH,
        headers={"X-Forwarded-Email": "ok@example.com"},
    )
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"


def test_request_without_proxy_email_falls_back_to_bearer(client_proxy_disabled):
    """No Authorization and no X-Forwarded-Email → 401 (existing bearer-mode behavior)."""
    r = client_proxy_disabled.get(PROTECTED_PATH)
    assert r.status_code == 401


def test_spoofed_proxy_header_from_untrusted_ip_is_rejected(client_proxy_untrusted_net):
    """X-Forwarded-Email from a non-trusted source IP must be ignored — 401."""
    r = client_proxy_untrusted_net.get(
        PROTECTED_PATH,
        headers={"X-Forwarded-Email": "spoofed@evil.com"},
    )
    assert r.status_code == 401


def test_trusted_proxy_without_email_fails_closed(client_proxy_trusted):
    """Trusted proxy net but X-Forwarded-Email missing → 401 (fail-closed).

    Failure mode: a misconfigured Caddy / oauth2-proxy that strips
    X-Forwarded-Email must NOT result in anonymous access. When trust is
    enabled and the request originates from inside the trusted proxy
    network but the header is absent, the dashboard must reject the
    request rather than silently accept it.
    """
    r = client_proxy_trusted.get(PROTECTED_PATH)
    assert r.status_code == 401


def test_trusted_proxy_empty_email_fails_closed(client_proxy_trusted):
    """Trusted proxy net with empty X-Forwarded-Email → 401 (fail-closed).

    Distinct from the missing-header case: an empty string can be sent by a
    misbehaving proxy, and `request.headers.get('X-Forwarded-Email')`
    returns `''` (falsy) rather than `None`. Both must reject.
    """
    r = client_proxy_trusted.get(
        PROTECTED_PATH,
        headers={"X-Forwarded-Email": ""},
    )
    assert r.status_code == 401
