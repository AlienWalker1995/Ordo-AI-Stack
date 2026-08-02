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


def test_sso_gate_is_shared_and_host_based(caddyfile_text: str) -> None:
    """All ported sites import the single (sso_forward_auth) snippet, whose rd=
    uses {host} (portless), NOT {hostport}.

    Why portless: the per-service Tailscale sidecar nodes (chat/dash/…
    .<tailnet>.ts.net) `serve`-forward their clean Host with no port, so the gate
    must match on the portless host — {hostport} would emit a bogus
    chat.<tailnet>:8443 rd and break sidecar sign-in. The one whitelisted
    wildcard domain in the edge plugin covers every resulting rd. If this flips
    back to {hostport}, the clean-URL sidecars lose SSO."""
    assert "(sso_forward_auth)" in caddyfile_text, "shared SSO snippet missing"
    assert "rd={scheme}://{host}{uri}" in caddyfile_text, "rd= must carry {host}"
    assert "rd={scheme}://{hostport}" not in caddyfile_text, (
        "rd= must NOT use {hostport} — it breaks the Tailscale sidecar clean URLs")
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


def test_legacy_paths_redirect_to_subdomains(caddyfile_text: str) -> None:
    """Old bookmarks must keep working AND land on the canonical URL: each pre-port
    subpath 302s to the clean per-service tailnet subdomain (chat/dash/hermes/…),
    not the :844x port. The port sites still exist as the sidecars' proxy target +
    a direct fallback, but the user-facing redirect targets are the subdomains.

    n8n is special: a bare `redir /n8n*` is FORBIDDEN because Caddy sorts redir
    before handle and it would shadow the :443 webhook/OAuth passthroughs. The
    real redirect goes through the @n8n_ui matcher (which excludes those paths),
    so assert the matcher-based redirect, not a literal `redir /n8n`."""
    for legacy, target in (
        ("/chat*", "https://chat.{$CADDY_TAILNET_DOMAIN}/"),
        ("/dash*", "https://dash.{$CADDY_TAILNET_DOMAIN}/"),
        ("/comfy*", "https://comfy.{$CADDY_TAILNET_DOMAIN}/"),
        ("/hermes*", "https://hermes.{$CADDY_TAILNET_DOMAIN}/"),
        ("/codebase-memory*", "https://graph.{$CADDY_TAILNET_DOMAIN}/"),
        ("/grafana*", "https://dash.{$CADDY_TAILNET_DOMAIN}/grafana/"),
    ):
        assert f"redir {legacy} {target} 302" in caddyfile_text, (
            f"legacy redirect for {legacy} must target the canonical subdomain {target}")
    # n8n: the real redirect is the matcher form targeting the n8n subdomain, NOT a bare `redir /n8n`.
    assert "redir @n8n_ui https://n8n.{$CADDY_TAILNET_DOMAIN}/ 302" in caddyfile_text, (
        "n8n legacy redirect must use the @n8n_ui matcher targeting the n8n subdomain — a bare "
        "`redir /n8n` would shadow the webhook/OAuth passthroughs")


# ── converged uniform-serving model (feat/clean-tailnet-urls) ───────────────────
# The three formerly-divergent UIs (n8n strip_prefix, Hermes X-Forwarded-Prefix,
# codebase-memory nginx sub_filter) now serve at their origin root behind a plain
# `import sso_service <upstream>` — identical to :8443/:8446. These invariants FAIL
# if any adapter (handle_path, redir-to-subpath bounce, X-Forwarded-Prefix) returns.

NGINX_CONF = REPO_ROOT / "services" / "codebase-memory-ui" / "nginx.conf"


def _site_block(text: str, port: str) -> str:
    """Return the body of the `:<port> { … }` site block (brace-matched)."""
    marker = f":{port} {{"
    start = text.index(marker) + len(marker)
    depth = 1
    i = start
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start:i - 1]


def test_formerly_divergent_ports_serve_at_root(caddyfile_text: str) -> None:
    """8445/8447/8448 each proxy at root via `import sso_service <upstream>` with
    no adapter — no handle_path, no `redir /` bounce to a subpath."""
    for port, upstream in (("8445", "n8n:5678"),
                           ("8447", "hermes-dashboard:9119"),
                           ("8448", "codebase-memory-ui:9750")):
        block = _site_block(caddyfile_text, port)
        assert f"import sso_service {upstream}" in block, (
            f":{port} must serve {upstream} via `import sso_service` at root")
        assert "handle_path" not in block, (
            f":{port} reintroduced handle_path — an adapter is back")
        assert "redir /" not in block, (
            f":{port} reintroduced a redir bounce — an adapter is back")


def test_no_forwarded_prefix_injection(caddyfile_text: str) -> None:
    """No site may inject X-Forwarded-Prefix — that was the Hermes subpath adapter."""
    assert "X-Forwarded-Prefix" not in caddyfile_text, (
        "X-Forwarded-Prefix reintroduced — the Hermes subpath adapter is back")


def test_codebase_memory_ui_serves_at_root() -> None:
    """services/codebase-memory-ui/nginx.conf must be a plain root proxy: no sub_filter
    rewrites, no /codebase-memory/ subpath, a `location /` at the root."""
    nginx = NGINX_CONF.read_text(encoding="utf-8")
    assert "sub_filter" not in nginx, "sub_filter reintroduced — the nginx subpath adapter is back"
    assert "/codebase-memory/" not in nginx, "/codebase-memory/ subpath reintroduced in nginx.conf"
    assert "location /" in nginx, "nginx.conf lost its root `location /` proxy"


def test_couchdb_livesync_bypasses_sso_and_blocks_fauxton(caddyfile_text: str) -> None:
    """The Obsidian Self-hosted LiveSync sync endpoint (/couchdb) must bypass Google SSO — the
    LiveSync clients are not browsers and authenticate to CouchDB directly (require_valid_user,
    set by the bridge). It lives on the :443 front door (a bypass route like /llm and /mcp), NOT
    behind the shared SSO gate. Fauxton (/_utils, the admin UI) must be blocked outright so it is
    never internet-reachable when the endpoint is exposed via Tailscale Funnel."""
    assert "handle_path /couchdb/*" in caddyfile_text, (
        "Caddyfile missing the /couchdb LiveSync route — Obsidian cross-device sync will fail.")
    assert "reverse_proxy couchdb:5984" in caddyfile_text, "/couchdb must proxy to couchdb:5984"
    assert "/couchdb/_utils" in caddyfile_text, (
        "Fauxton (/couchdb/_utils) must be explicitly blocked — the CouchDB admin UI must never be "
        "internet-reachable via Funnel.")
    # on the :443 front door, not inside an :844x SSO-gated port block
    root_site = caddyfile_text.split("{$CADDY_TAILNET_HOSTNAME} {", 1)[1]
    assert "handle_path /couchdb/*" in root_site, "/couchdb must be on the :443 front door (SSO bypass)"
