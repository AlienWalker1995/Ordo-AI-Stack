"""Static invariants of `auth/caddy/Caddyfile`.

These are cheap grep-style assertions on textual content — no Caddy adapter
or docker daemon required — that guard the security-fragile lines we'd
notice only at integration smoke time. They exist because the n8n OAuth
callback / webhook bypass list is the most easily-broken line in the
Caddyfile (a typo here either breaks Google OAuth callbacks for n8n, or
accidentally exempts a wider path than intended).

If any of these tests fail, treat it as a regression of an explicit
security guarantee — not a refactor opportunity.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CADDYFILE = REPO_ROOT / "auth" / "caddy" / "Caddyfile"


@pytest.fixture(scope="module")
def caddyfile_text() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


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


def test_caddy_tls_uses_tailscale_cert(caddyfile_text: str) -> None:
    """Caddy must use the Tailscale-issued cert mounted at /etc/caddy/certs/,
    not attempt ACME auto-https against the .ts.net hostname (which would
    fail and lock out the front door on cert-renewal day)."""
    assert "auto_https off" in caddyfile_text
    assert "/etc/caddy/certs/tailnet.crt" in caddyfile_text
    assert "/etc/caddy/certs/tailnet.key" in caddyfile_text


def test_caddy_renames_xauthrequest_to_xforwarded(caddyfile_text: str) -> None:
    """Caddy `copy_headers Source>Target` syntax renames oauth2-proxy's
    X-Auth-Request-* headers into the X-Forwarded-* names that the
    dashboard's _verify_auth() reads."""
    assert "X-Auth-Request-Email>X-Forwarded-Email" in caddyfile_text


def test_no_ai_toolkit_references(caddyfile_text: str) -> None:
    """ai-toolkit was retired 2026-07-24. :8443 has since been REASSIGNED to Open WebUI
    (port-per-service model) — so the port existing is fine; a reference to the dead
    ai-toolkit upstream is not."""
    assert "ai-toolkit" not in caddyfile_text, "stale ai-toolkit reference in Caddyfile"


# ── port-per-service model (2026-07-24) ─────────────────────────────────────────
# Every UI service gets its own SSO-gated port; :443 is the front door. These
# invariants guard the two ways that model can silently rot: a service port
# losing its forward_auth gate (exposes the UI to the tailnet with no SSO), and
# the rd= redirect using {host} instead of {hostport} (strands sign-ins on :443).

SERVICE_PORTS = {
    "8443": "open-webui:8080",
    "8444": "dashboard:8080",
    "8445": "n8n:5678",
    "8446": "comfyui:8188",
    "8447": "hermes-dashboard:9119",
    "8448": "codebase-memory-ui:9750",
}


def test_every_service_port_has_a_site(caddyfile_text: str) -> None:
    for port, upstream in SERVICE_PORTS.items():
        assert f":{port} {{" in caddyfile_text, f"missing site block for :{port}"
        assert upstream in caddyfile_text, f"missing upstream {upstream}"


def test_sso_gate_is_shared_and_port_aware(caddyfile_text: str) -> None:
    """All ported sites import the single (sso_forward_auth) snippet, whose rd=
    uses {hostport} so sign-in returns to the originating port. If the snippet
    or its {hostport} disappears, every ported UI either loses SSO or strands
    users on :443 after Google sign-in."""
    assert "(sso_forward_auth)" in caddyfile_text, "shared SSO snippet missing"
    assert "rd={scheme}://{hostport}{uri}" in caddyfile_text, "rd= must carry {hostport}"
    # every service site pulls the gate in
    assert caddyfile_text.count("import sso_forward_auth") + \
        caddyfile_text.count("import sso_service") >= len(SERVICE_PORTS) + 1, (
        "fewer SSO-gate imports than SSO-gated sites — a ported UI lost its gate")


def test_root_no_longer_serves_a_ui_catchall(caddyfile_text: str) -> None:
    """:443's route block must NOT end in a reverse_proxy catch-all to a UI app
    (the old open-webui-at-root model). The landing page + 404 are the only
    unmatched-path handlers on :443."""
    root_site = caddyfile_text.split("{$CADDY_TAILNET_HOSTNAME} {", 1)[1]
    tail = root_site[root_site.rfind("handle {"):]
    assert "reverse_proxy" not in tail, ":443 catch-all proxies a UI again — port model regressed"


def test_legacy_paths_redirect_to_ports(caddyfile_text: str) -> None:
    """Old bookmarks must keep working: each pre-port subpath 302s to its port."""
    for legacy, port in (("/chat", "8443"), ("/dash", "8444"), ("/n8n", "8445"),
                         ("/comfy", "8446"), ("/hermes", "8447"), ("/codebase-memory", "8448")):
        assert f"redir {legacy}" in caddyfile_text, f"missing legacy redirect for {legacy}"
        assert f":{port}" in caddyfile_text
