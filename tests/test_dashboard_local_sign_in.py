"""Local-mode sign-in: the operator's way into the dashboard when there is no SSO edge.

With the edge off (ordo/render/engine.py), the render publishes the dashboard on 127.0.0.1:8444 and hands
it DASHBOARD_LOCAL_LOGIN_TOKEN, a secret only the operator (secrets.env, printed by `ordo up`) and
the dashboard hold. The browser exchanges it once for a signed, HttpOnly, SameSite=Strict session
cookie. Every other container on the stack network still has no principal: it holds neither the
token nor a cookie minted from it, so #248's confused-deputy guarantee holds in local mode too.

With the edge on, the render does not pass the token, and the dashboard behaves exactly as #248.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

OPS_TOKEN = "test-ops-controller-token"
LOCAL_TOKEN = "test-local-login-token-0123456789abcdef"
CADDY_IP = "10.9.0.7"
OTHER_CONTAINER_IP = "10.9.0.42"
# What a request through the loopback-published port looks like from inside the container: the
# peer is the Docker gateway / proxy, never something the dashboard can tell apart by address.
PUBLISHED_PORT_PEER_IP = "10.9.0.1"
PROTECTED = "/api/ops/services/searxng/restart"
SIGN_IN = "/api/auth/local/sign-in"
SESSION = "/api/auth/session"


@pytest.fixture
def ops_http(monkeypatch):
    import dashboard.app  # noqa: F401  (load the module before patching it)

    ops_response = MagicMock(status_code=200)
    ops_response.json.return_value = {"ok": True}
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    mock_client.request = AsyncMock(return_value=ops_response)
    monkeypatch.setattr("dashboard.app._http_client", mock_client)
    return mock_client


def _configure(monkeypatch, local_token: str) -> None:
    import dashboard.app as dashboard_app
    import dashboard.auth as auth
    import dashboard.settings as settings

    monkeypatch.setattr(settings, "OPS_CONTROLLER_TOKEN", OPS_TOKEN)
    monkeypatch.setattr(dashboard_app, "OPS_CONTROLLER_TOKEN", OPS_TOKEN)
    monkeypatch.setattr(settings, "DASHBOARD_LOCAL_LOGIN_TOKEN", local_token)
    monkeypatch.setattr(auth, "edge_proxy_addresses", lambda: {CADDY_IP})


@pytest.fixture
def local_mode(ops_http, monkeypatch):
    """The render published the dashboard on loopback and passed it the local sign-in token."""
    _configure(monkeypatch, LOCAL_TOKEN)


@pytest.fixture
def edge_mode(ops_http, monkeypatch):
    """The render enabled the edge: no local sign-in token reaches the dashboard."""
    _configure(monkeypatch, "")


def _client(peer_ip: str) -> TestClient:
    import dashboard.app as dashboard_app

    return TestClient(dashboard_app.app, client=(peer_ip, 50000))


def _signed_in_operator() -> TestClient:
    client = _client(PUBLISHED_PORT_PEER_IP)
    r = client.post(SIGN_IN, json={"token": LOCAL_TOKEN})
    assert r.status_code == 204, r.text
    return client


class TestLocalModeRefusesInternalCallers:
    def test_anonymous_internal_call_is_refused(self, local_mode, ops_http):
        r = _client(OTHER_CONTAINER_IP).post(PROTECTED)
        assert r.status_code == 401
        ops_http.request.assert_not_awaited()

    def test_anonymous_call_through_the_published_port_is_refused(self, local_mode):
        """Arriving via the loopback port proves nothing by itself: the peer is a gateway address."""
        assert _client(PUBLISHED_PORT_PEER_IP).post(PROTECTED).status_code == 401

    def test_wrong_token_is_refused_and_sets_no_cookie(self, local_mode):
        client = _client(OTHER_CONTAINER_IP)
        r = client.post(SIGN_IN, json={"token": "guess"})
        assert r.status_code == 401
        assert "set-cookie" not in r.headers
        assert client.post(PROTECTED).status_code == 401

    def test_missing_token_is_refused(self, local_mode):
        assert _client(OTHER_CONTAINER_IP).post(SIGN_IN, json={}).status_code in (401, 422)

    def test_form_posted_token_is_not_accepted(self, local_mode):
        """Only a JSON body signs in, so a cross-site HTML form cannot drive the endpoint."""
        r = _client(OTHER_CONTAINER_IP).post(SIGN_IN, data={"token": LOCAL_TOKEN})
        assert r.status_code != 204
        assert "set-cookie" not in r.headers

    def test_forged_cookie_is_refused(self, local_mode):
        client = _client(OTHER_CONTAINER_IP)
        expires = int(time.time()) + 3600
        client.cookies.set("ordo_local_session", f"{expires}.{'0' * 64}")
        assert client.post(PROTECTED).status_code == 401

    def test_cookie_from_a_rotated_token_is_refused(self, local_mode, monkeypatch):
        import dashboard.settings as settings

        client = _signed_in_operator()
        monkeypatch.setattr(settings, "DASHBOARD_LOCAL_LOGIN_TOKEN", "a-rotated-token-value-0123456789")
        assert client.post(PROTECTED).status_code == 401

    def test_expired_cookie_is_refused(self, local_mode):
        import dashboard.auth as auth

        client = _client(OTHER_CONTAINER_IP)
        expired = auth.local_session_cookie(LOCAL_TOKEN, now=time.time() - auth.LOCAL_SESSION_SECONDS - 5)
        client.cookies.set(auth.LOCAL_SESSION_COOKIE, expired)
        assert client.post(PROTECTED).status_code == 401

    def test_edge_identity_header_still_needs_the_edge_peer(self, local_mode):
        r = _client(OTHER_CONTAINER_IP).post(PROTECTED, headers={"X-Forwarded-Email": "op@example.com"})
        assert r.status_code == 401


class TestLocalOperator:
    def test_sign_in_sets_a_hardened_session_cookie(self, local_mode):
        r = _client(PUBLISHED_PORT_PEER_IP).post(SIGN_IN, json={"token": LOCAL_TOKEN})
        assert r.status_code == 204
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("ordo_local_session=")
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie or "SameSite=Strict" in cookie
        assert "Path=/" in cookie
        assert LOCAL_TOKEN not in cookie

    def test_signed_in_operator_can_act(self, local_mode, ops_http):
        r = _signed_in_operator().post(PROTECTED)
        assert r.status_code == 200, r.text
        ops_http.request.assert_awaited_once()

    def test_session_reports_local_mode_and_state(self, local_mode):
        anonymous = _client(PUBLISHED_PORT_PEER_IP).get(SESSION)
        assert anonymous.status_code == 200
        assert anonymous.json() == {"mode": "local", "signed_in": False}
        assert _signed_in_operator().get(SESSION).json() == {"mode": "local", "signed_in": True}

    def test_bearer_still_works_in_local_mode(self, local_mode):
        r = _client(OTHER_CONTAINER_IP).post(PROTECTED, headers={"Authorization": f"Bearer {OPS_TOKEN}"})
        assert r.status_code == 200


class TestEdgeModeIsUnchanged:
    def test_sign_in_does_not_exist(self, edge_mode):
        r = _client(OTHER_CONTAINER_IP).post(SIGN_IN, json={"token": ""})
        assert r.status_code == 404
        assert "set-cookie" not in r.headers

    def test_a_session_cookie_is_ignored(self, edge_mode):
        import dashboard.auth as auth

        client = _client(OTHER_CONTAINER_IP)
        # Even a cookie correctly signed with some token carries no weight without local mode.
        client.cookies.set(auth.LOCAL_SESSION_COOKIE, auth.local_session_cookie(LOCAL_TOKEN, now=time.time()))
        assert client.post(PROTECTED).status_code == 401

    def test_edge_identity_from_caddy_is_accepted(self, edge_mode, ops_http):
        r = _client(CADDY_IP).post(PROTECTED, headers={"X-Forwarded-Email": "operator@example.com"})
        assert r.status_code == 200, r.text

    def test_session_reports_edge_mode(self, edge_mode):
        assert _client(CADDY_IP).get(SESSION).json() == {"mode": "edge", "signed_in": False}
        signed = _client(CADDY_IP).get(SESSION, headers={"X-Forwarded-Email": "operator@example.com"})
        assert signed.json() == {"mode": "edge", "signed_in": True}
