"""Static invariants of `auth/caddy/Caddyfile` and `docker-compose.yml`.

These are cheap grep-style assertions on textual content — no Caddy adapter
or docker daemon required — that guard the security-fragile lines we'd
notice only at integration smoke time. They exist because:

* the n8n OAuth callback / webhook bypass list is the most easily-broken
  line in the Caddyfile (a typo here either breaks Google OAuth callbacks
  for n8n, or accidentally exempts a wider path than intended); and
* the `CADDY_BIND` failsafe in compose is the only thing standing between
  a misconfigured operator and a `0.0.0.0:443` Caddy bind.

If any of these tests fail, treat it as a regression of an explicit
security guarantee — not a refactor opportunity.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = REPO_ROOT / "auth" / "caddy" / "Caddyfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"


@pytest.fixture(scope="module")
def caddyfile_text() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def test_oauth2_endpoints_exempt_from_auth(caddyfile_text: str) -> None:
    """The /oauth2/* dance (start, callback, sign_out) must bypass forward_auth."""
    assert "/oauth2/*" in caddyfile_text, (
        "Caddyfile missing /oauth2/* exemption; the OIDC sign-in flow cannot "
        "complete without it."
    )


def test_healthz_exempt_from_auth(caddyfile_text: str) -> None:
    """/healthz must remain reachable without a session for liveness probes."""
    assert "/healthz" in caddyfile_text


def test_n8n_oauth_callback_bypasses_sso(caddyfile_text: str) -> None:
    """External OAuth providers (Google, Notion, etc.) call back to n8n at
    /n8n/rest/oauth2-credential/callback. Caddy must NOT challenge that
    path with SSO — the caller has no session cookie."""
    assert "/n8n/rest/oauth2-credential/callback" in caddyfile_text, (
        "Caddyfile missing the n8n OAuth callback bypass — external OAuth "
        "flows into n8n will fail."
    )


def test_n8n_webhook_bypasses_sso(caddyfile_text: str) -> None:
    """n8n /webhook/* triggers must remain reachable without SSO so external
    services (Stripe, Linear, etc.) can fire workflows."""
    assert "/n8n/webhook/*" in caddyfile_text, (
        "Caddyfile missing the n8n /webhook/* bypass — external webhooks "
        "into n8n will fail."
    )


def test_caddy_bind_failsafe_is_required(compose_text: str) -> None:
    """`${CADDY_BIND:?…}` makes compose refuse to start with empty CADDY_BIND.
    Without it, an empty CADDY_BIND silently degrades to 0.0.0.0:443 and
    Caddy publishes on every host interface. This guard is non-optional."""
    assert "${CADDY_BIND:?" in compose_text, (
        "docker-compose.yml lost the CADDY_BIND :? failsafe — empty values "
        "would now silently bind to 0.0.0.0."
    )


def test_caddy_tls_uses_tailscale_cert(caddyfile_text: str) -> None:
    """Caddy must use the Tailscale-issued cert mounted at /etc/caddy/certs/,
    not attempt ACME auto-https against the .ts.net hostname (which would
    fail and lock out the front door on cert-renewal day)."""
    assert "auto_https off" in caddyfile_text
    assert "/etc/caddy/certs/tailnet.crt" in caddyfile_text
    assert "/etc/caddy/certs/tailnet.key" in caddyfile_text


def test_oauth2_proxy_emits_xauthrequest_headers(compose_text: str) -> None:
    """oauth2-proxy emits X-Auth-Request-Email/User/Preferred-Username on
    /oauth2/auth only when --set-xauthrequest=true. Caddy renames those
    to X-Forwarded-Email/User/Preferred-Username for upstreams. Without
    this flag the dashboard sees no email and fails closed (401)."""
    assert "--set-xauthrequest=true" in compose_text


def test_caddy_renames_xauthrequest_to_xforwarded(caddyfile_text: str) -> None:
    """Caddy `copy_headers Source>Target` syntax renames oauth2-proxy's
    X-Auth-Request-* headers into the X-Forwarded-* names that the
    dashboard's _verify_auth() reads."""
    assert "X-Auth-Request-Email>X-Forwarded-Email" in caddyfile_text
