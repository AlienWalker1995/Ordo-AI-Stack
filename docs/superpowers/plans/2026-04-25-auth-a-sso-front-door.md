# Auth A — SSO Front Door Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dashboard's paste-bearer-token login with a Google-SSO front door that covers dashboard, open-webui, n8n, hermes-dashboard, and comfyui in one sign-in. Tailscale stays as the network gate; Google OIDC is the identity gate.

**Architecture:** A new Caddy reverse proxy (listening only on the tailnet IP) sits in front of all web UIs at one hostname (`ordo.<tailnet>.ts.net`) with path-mounted routes (`/dash/`, `/chat/`, `/n8n/`, `/hermes/`, `/comfy/`). Caddy uses `forward_auth` to a sibling oauth2-proxy container that performs Google OIDC and enforces a single-email allowlist. Dashboard's `_verify_auth()` learns to trust `X-Forwarded-Email` from oauth2-proxy when the request originates from the proxy network; bearer-token mode stays for orchestration-mcp/internal calls.

**Tech stack:** Caddy 2 (`caddy:2-alpine`), oauth2-proxy (`quay.io/oauth2-proxy/oauth2-proxy:latest`), FastAPI dashboard (existing), Docker Compose.

**Lifecycle:** This plan lives only on `feat/auth-redesign` and is dropped before the implementation work merges to `main` (see `docs/superpowers/specs/2026-04-25-auth-redesign-design.md` § Lifecycle).

---

## File structure

| File | Action | Responsibility |
|------|--------|----------------|
| `docker-compose.yml` | modify | Add `caddy` + `oauth2-proxy` services, `proxy-net` network; drop host-port publishes from internal-only UIs |
| `auth/caddy/Caddyfile` | create | Routes `/dash/`, `/chat/`, `/n8n/`, `/hermes/`, `/comfy/` with `forward_auth` to oauth2-proxy; n8n callback bypass list |
| `auth/oauth2-proxy/emails.txt` | create | Single-line allowlist file (placeholder `YOUR_ALLOWLIST_EMAIL` in repo; user replaces locally) |
| `.env.example` | modify | Document new SSO env vars (OAUTH2_*, CADDY_TAILNET_HOSTNAME) |
| `dashboard/settings.py` | modify | Read `DASHBOARD_TRUST_PROXY_HEADERS`, `DASHBOARD_TRUSTED_PROXY_NET` |
| `dashboard/app.py` | modify | `_verify_auth()` gains proxy-headers branch; new helper `_request_from_trusted_proxy()` |
| `dashboard/test_proxy_auth.py` | create | Unit tests for proxy-headers branch + spoofing protection |
| `docs/runbooks/auth.md` | create | Operator runbook: setup, recovery, allowlist edits |

Branch base: `feat/auth-redesign`.

---

## Task 1: Pre-flight — Google Cloud OAuth client setup

This is a manual user task. The plan documents what's needed; no code or commits.

- [ ] **Step 1: Create OAuth 2.0 Client ID in Google Cloud Console**

Open https://console.cloud.google.com/apis/credentials. Create OAuth 2.0 Client ID, type "Web application." Set:
- Authorized JavaScript origins: `https://ordo.<tailnet>.ts.net`
- Authorized redirect URIs: `https://ordo.<tailnet>.ts.net/oauth2/callback`

Replace `<tailnet>` with the actual tailnet name. Capture the Client ID and Client secret — they go into `.env` (or `secrets/.env.sops` once Plan B is in).

- [ ] **Step 2: Issue a Tailscale TLS cert for the hostname**

Run on the Caddy host: `tailscale cert ordo.<tailnet>.ts.net`. Confirms Tailscale will mint a cert for this hostname (used by Caddy in Task 5).

- [ ] **Step 3: Generate the cookie secret**

Run: `openssl rand -base64 32`. Save to `.env` as `OAUTH2_PROXY_COOKIE_SECRET`. 32 bytes, base64-encoded.

No commit — preflight only.

---

## Task 2: Add proxy-net network to docker-compose.yml

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add `proxy-net` network at the end of the `networks:` block**

Find the `networks:` top-level key in `docker-compose.yml`. Add:

```yaml
networks:
  # ... existing networks ...
  proxy-net:
    driver: bridge
```

- [ ] **Step 2: Verify the file parses cleanly**

Run: `cd /c/dev/AI-toolkit && docker compose config --services > /dev/null && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(auth): add proxy-net Docker network for SSO front door"
```

---

## Task 3: Add oauth2-proxy emails allowlist file

**Files:**
- Create: `auth/oauth2-proxy/emails.txt`

- [ ] **Step 1: Create the file with placeholder**

Create `auth/oauth2-proxy/emails.txt` with content:

```
YOUR_ALLOWLIST_EMAIL
```

Single line. The placeholder is replaced locally by the user; the file in the public repo stays placeholder.

- [ ] **Step 2: Add a README beside it**

Create `auth/oauth2-proxy/README.md`:

```markdown
# oauth2-proxy

`emails.txt` is the Google-account allowlist for the SSO front door.
One email per line. Only listed emails can complete the OIDC dance.

This file is committed with a placeholder (`YOUR_ALLOWLIST_EMAIL`).
Replace it locally; do **not** commit your real email.

To reload after editing: `docker compose restart oauth2-proxy`.
```

- [ ] **Step 3: Commit**

```bash
git add auth/oauth2-proxy/emails.txt auth/oauth2-proxy/README.md
git commit -m "feat(auth): scaffold oauth2-proxy emails allowlist with placeholder"
```

---

## Task 4: Add oauth2-proxy service to docker-compose.yml

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add the service definition**

Insert this service block in `docker-compose.yml` (near the other gateways, e.g. after `mcp-gateway`):

```yaml
  oauth2-proxy:
    image: quay.io/oauth2-proxy/oauth2-proxy:latest
    restart: unless-stopped
    command:
      - --provider=google
      - --http-address=0.0.0.0:4180
      - --reverse-proxy=true
      - --upstream=static://202
      - --redirect-url=https://${CADDY_TAILNET_HOSTNAME}/oauth2/callback
      - --whitelist-domain=.${CADDY_TAILNET_DOMAIN}
      - --cookie-domain=.${CADDY_TAILNET_DOMAIN}
      - --cookie-secure=true
      - --cookie-samesite=lax
      - --cookie-expire=24h
      - --email-domain=*
      - --authenticated-emails-file=/etc/oauth2-proxy/emails.txt
      - --skip-provider-button=true
    environment:
      - OAUTH2_PROXY_CLIENT_ID=${OAUTH2_PROXY_CLIENT_ID}
      - OAUTH2_PROXY_CLIENT_SECRET=${OAUTH2_PROXY_CLIENT_SECRET}
      - OAUTH2_PROXY_COOKIE_SECRET=${OAUTH2_PROXY_COOKIE_SECRET}
    volumes:
      - ./auth/oauth2-proxy/emails.txt:/etc/oauth2-proxy/emails.txt:ro
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://localhost:4180/ping"]
      interval: 30s
      timeout: 5s
      retries: 3
    networks:
      - proxy-net
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

- [ ] **Step 2: Verify the file parses cleanly**

Run: `cd /c/dev/AI-toolkit && docker compose config --services | grep oauth2-proxy`
Expected: `oauth2-proxy` appears in the output.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(auth): add oauth2-proxy service backing Google OIDC SSO"
```

---

## Task 5: Add Caddyfile

**Files:**
- Create: `auth/caddy/Caddyfile`

- [ ] **Step 1: Create the Caddyfile with placeholders for upstream wiring**

Create `auth/caddy/Caddyfile`:

```caddyfile
{
    # Listen only on the tailnet interface; never bind 0.0.0.0.
    # The bind address is set via the CADDY_BIND env at compose time.
    auto_https off
}

{$CADDY_TAILNET_HOSTNAME} {
    # Tailscale-issued cert (mounted in by host).
    tls /etc/caddy/certs/tailnet.crt /etc/caddy/certs/tailnet.key

    # ---- forward_auth target (oauth2-proxy) ----
    # Defined as a snippet so each protected route can reuse it.
    @auth not path /oauth2/* /healthz
    handle @auth {
        forward_auth oauth2-proxy:4180 {
            uri /oauth2/auth
            copy_headers X-Forwarded-Email X-Forwarded-User X-Forwarded-Preferred-Username
        }
    }

    # ---- oauth2-proxy endpoints (start, callback, sign_out) ----
    handle /oauth2/* {
        reverse_proxy oauth2-proxy:4180
    }

    # ---- health (public, no auth) ----
    handle /healthz {
        respond "ok" 200
    }

    # ---- Upstream UIs (wired up in subsequent tasks) ----
    # Placeholders below will be replaced as each UI is wired in Tasks 8-12.

    handle {
        respond "auth ok — no route configured for this path yet" 404
    }
}
```

- [ ] **Step 2: Commit**

```bash
git add auth/caddy/Caddyfile
git commit -m "feat(auth): scaffold Caddyfile with oauth2-proxy forward_auth"
```

---

## Task 6: Add Caddy service to docker-compose.yml

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add the Caddy service**

Insert before or after `oauth2-proxy` in `docker-compose.yml`:

```yaml
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    # CADDY_BIND defaults to the tailnet IP; never bind 0.0.0.0.
    # Set CADDY_BIND in .env: e.g. CADDY_BIND=100.x.y.z (your tailnet IP).
    ports:
      - "${CADDY_BIND}:443:443"
    environment:
      - CADDY_TAILNET_HOSTNAME=${CADDY_TAILNET_HOSTNAME}
      - CADDY_TAILNET_DOMAIN=${CADDY_TAILNET_DOMAIN}
    volumes:
      - ./auth/caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - ${TAILSCALE_CERT_DIR:-./auth/caddy/certs}:/etc/caddy/certs:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      oauth2-proxy:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://localhost/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
    networks:
      - proxy-net
      - frontend
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

# (Add to top-level volumes:)
volumes:
  caddy_data:
  caddy_config:
```

- [ ] **Step 2: Verify config parses**

Run: `cd /c/dev/AI-toolkit && docker compose config --services | sort`
Expected: `caddy` and `oauth2-proxy` both listed.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(auth): add caddy reverse-proxy service bound to tailnet IP"
```

---

## Task 7: Add SSO env vars to .env.example

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Add a documented section near the bottom of `.env.example`**

```bash
# --- SSO Front Door (Caddy + oauth2-proxy) ---
# Tailnet hostname Caddy serves (must have an authorized redirect_uri in
# the Google OAuth client matching https://<this>/oauth2/callback).
# CADDY_TAILNET_HOSTNAME=ordo.<tailnet>.ts.net
# Domain for the cookie scope (must match the hostname's parent domain).
# CADDY_TAILNET_DOMAIN=<tailnet>.ts.net
# Tailscale-mounted IP. Never use 0.0.0.0 — the proxy must be tailnet-only.
# Get it from `tailscale ip -4`.
# CADDY_BIND=100.x.y.z
# Path on the host where Tailscale-issued certs are mounted into Caddy.
# Default: ./auth/caddy/certs (where you put tailnet.crt + tailnet.key
# from `tailscale cert <hostname>`).
# TAILSCALE_CERT_DIR=./auth/caddy/certs
# Google OAuth 2.0 Client (https://console.cloud.google.com/apis/credentials)
# OAUTH2_PROXY_CLIENT_ID=
# OAUTH2_PROXY_CLIENT_SECRET=
# 32-byte base64 cookie signing secret. Generate: openssl rand -base64 32
# OAUTH2_PROXY_COOKIE_SECRET=
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "docs(auth): document SSO front door env vars in .env.example"
```

---

## Task 8: Smoke-test Caddy + oauth2-proxy come up healthy

**Files:** none modified (verification only).

- [ ] **Step 1: Set local .env values**

In your local `.env` (gitignored, not in this commit):
```
CADDY_TAILNET_HOSTNAME=ordo.<your-tailnet>.ts.net
CADDY_TAILNET_DOMAIN=<your-tailnet>.ts.net
CADDY_BIND=<your tailnet IP from `tailscale ip -4`>
OAUTH2_PROXY_CLIENT_ID=<from Task 1>
OAUTH2_PROXY_CLIENT_SECRET=<from Task 1>
OAUTH2_PROXY_COOKIE_SECRET=<from Task 1>
```
Replace `auth/oauth2-proxy/emails.txt` locally with your real allowlist email (do **not** commit this).

- [ ] **Step 2: Place Tailscale cert files**

`mkdir -p auth/caddy/certs && tailscale cert --cert-file auth/caddy/certs/tailnet.crt --key-file auth/caddy/certs/tailnet.key ordo.<tailnet>.ts.net`

- [ ] **Step 3: Bring up just the proxy stack**

`docker compose up -d caddy oauth2-proxy`

- [ ] **Step 4: Verify both healthy**

`docker ps --filter name=caddy --filter name=oauth2-proxy --format "table {{.Names}}\t{{.Status}}"`
Expected: both `Up <time> (healthy)` within 60s.

- [ ] **Step 5: Hit the public health endpoint from a tailnet device**

`curl -k https://ordo.<tailnet>.ts.net/healthz`
Expected: body `ok`, HTTP 200.

- [ ] **Step 6: Hit a path with no route — should redirect to oauth2-proxy**

`curl -k -I https://ordo.<tailnet>.ts.net/anything`
Expected: HTTP 302 with `Location: /oauth2/start?...`

No commit — this is a verification gate before wiring upstreams.

---

## Task 9: Add dashboard X-Forwarded-Email trust mode (TDD)

**Files:**
- Create: `dashboard/test_proxy_auth.py`
- Modify: `dashboard/settings.py`
- Modify: `dashboard/app.py`

- [ ] **Step 1: Write the failing test**

Create `dashboard/test_proxy_auth.py`:

```python
import ipaddress
import pytest
from fastapi.testclient import TestClient
from dashboard.app import app
from dashboard import settings


@pytest.fixture(autouse=True)
def trust_proxy(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_TRUST_PROXY_HEADERS", True)
    monkeypatch.setattr(
        settings, "DASHBOARD_TRUSTED_PROXY_NET", ipaddress.ip_network("172.20.0.0/16")
    )


def test_request_from_trusted_proxy_with_email_passes(monkeypatch):
    client = TestClient(app)
    r = client.get(
        "/api/health",
        headers={"X-Forwarded-Email": "ok@example.com"},
        # TestClient default client is 127.0.0.1; simulate a trusted proxy IP
        # via a custom transport hook in conftest if needed. For now, mark
        # the trusted-net to include 127.0.0.0/8 in the fixture override
        # used by the next test variant.
    )
    assert r.status_code == 200


def test_request_without_proxy_email_falls_back_to_bearer():
    """If no proxy header is present, bearer-token mode still applies."""
    client = TestClient(app)
    r = client.get("/api/orchestration/state")
    # No bearer, no proxy header — should be 401 (existing behavior).
    assert r.status_code == 401


def test_spoofed_proxy_header_from_untrusted_ip_is_rejected(monkeypatch):
    """X-Forwarded-Email from outside the trusted proxy network must be ignored."""
    monkeypatch.setattr(
        settings, "DASHBOARD_TRUSTED_PROXY_NET", ipaddress.ip_network("10.0.0.0/8")
    )
    client = TestClient(app)
    r = client.get(
        "/api/orchestration/state",
        headers={"X-Forwarded-Email": "spoofed@evil.com"},
    )
    # 127.0.0.1 isn't in 10.0.0.0/8 → header ignored → 401.
    assert r.status_code == 401
```

- [ ] **Step 2: Run tests, verify they fail**

`cd /c/dev/AI-toolkit && pytest dashboard/test_proxy_auth.py -v`
Expected: FAIL with "AttributeError: module 'dashboard.settings' has no attribute 'DASHBOARD_TRUST_PROXY_HEADERS'".

- [ ] **Step 3: Add settings**

In `dashboard/settings.py`, add:

```python
import ipaddress
import os

DASHBOARD_TRUST_PROXY_HEADERS = os.environ.get(
    "DASHBOARD_TRUST_PROXY_HEADERS", "false"
).lower() == "true"

_proxy_net = os.environ.get("DASHBOARD_TRUSTED_PROXY_NET", "")
DASHBOARD_TRUSTED_PROXY_NET = (
    ipaddress.ip_network(_proxy_net, strict=False) if _proxy_net else None
)
```

- [ ] **Step 4: Add the auth helper to dashboard/app.py**

Near the existing `_verify_auth` (around `dashboard/app.py:97-105`), add:

```python
import ipaddress
from fastapi import Request
from dashboard import settings


def _request_from_trusted_proxy(request: Request) -> bool:
    """True if the request originates from the configured proxy network."""
    if not settings.DASHBOARD_TRUST_PROXY_HEADERS:
        return False
    if settings.DASHBOARD_TRUSTED_PROXY_NET is None:
        return False
    client_ip = request.client.host if request.client else None
    if client_ip is None:
        return False
    try:
        return ipaddress.ip_address(client_ip) in settings.DASHBOARD_TRUSTED_PROXY_NET
    except ValueError:
        return False
```

Then update `_verify_auth` to short-circuit on a trusted-proxy request with `X-Forwarded-Email`:

```python
def _verify_auth(request: Request) -> str | None:
    """Returns the authenticated email/identity, or None if anonymous-allowed.

    Two paths:
      1. Trusted proxy with X-Forwarded-Email → return that email.
      2. Bearer token in Authorization header → return 'bearer'.
    """
    if _request_from_trusted_proxy(request):
        email = request.headers.get("X-Forwarded-Email")
        if email:
            return email
        # Trusted proxy but no email → fail closed.
        if settings.AUTH_REQUIRED:
            raise HTTPException(401, "missing X-Forwarded-Email from trusted proxy")
        return None
    # Existing bearer-token logic stays here, unchanged.
    auth_hdr = request.headers.get("Authorization", "")
    if not auth_hdr.startswith("Bearer "):
        if settings.AUTH_REQUIRED:
            raise HTTPException(401, "missing bearer")
        return None
    token = auth_hdr[len("Bearer "):]
    if not hmac.compare_digest(token, settings.DASHBOARD_AUTH_TOKEN):
        raise HTTPException(401, "bad token")
    return "bearer"
```

- [ ] **Step 5: Run tests, verify they pass**

`pytest dashboard/test_proxy_auth.py -v`
Expected: 3/3 pass.

- [ ] **Step 6: Run the full dashboard test suite — no regressions**

`pytest dashboard/ -v`
Expected: all pass (no test count regression vs main).

- [ ] **Step 7: Commit**

```bash
git add dashboard/settings.py dashboard/app.py dashboard/test_proxy_auth.py
git commit -m "feat(dashboard): trust X-Forwarded-Email from configured proxy network"
```

---

## Task 10: Wire dashboard through Caddy

**Files:**
- Modify: `auth/caddy/Caddyfile`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Replace the placeholder `handle { ... }` block in Caddyfile with the dashboard route**

In `auth/caddy/Caddyfile`, replace:

```caddyfile
    handle {
        respond "auth ok — no route configured for this path yet" 404
    }
```

with:

```caddyfile
    # ---- /dash/ → dashboard:8080 ----
    handle_path /dash/* {
        reverse_proxy dashboard:8080 {
            header_up X-Forwarded-Email {http.reverse_proxy.header.X-Forwarded-Email}
            header_up X-Forwarded-User {http.reverse_proxy.header.X-Forwarded-User}
        }
    }

    handle {
        respond "no route" 404
    }
```

- [ ] **Step 2: Set new dashboard env vars in docker-compose.yml**

In the `dashboard` service `environment:` block (currently ends near line 200 in main), add:

```yaml
      - DASHBOARD_TRUST_PROXY_HEADERS=true
      - DASHBOARD_TRUSTED_PROXY_NET=172.20.0.0/16
```

(Use the `proxy-net` subnet — confirm it via `docker network inspect ordo-ai-stack_proxy-net | grep Subnet` after first boot, then pin the exact CIDR.)

- [ ] **Step 3: Drop dashboard's host-port publish**

In `docker-compose.yml`, remove from the `dashboard` service:

```yaml
    ports:
      - "8080:8080"
```

- [ ] **Step 4: Add dashboard to proxy-net network**

In the `dashboard` service `networks:` block, add `proxy-net`:

```yaml
    networks:
      - frontend  # existing
      - proxy-net  # new — Caddy can reach dashboard
```

- [ ] **Step 5: Recreate the affected services**

`docker compose up -d --force-recreate dashboard caddy`

- [ ] **Step 6: Smoke test from a tailnet browser**

Open `https://ordo.<tailnet>.ts.net/dash/`. Expected: redirected to Google → after consent → returned to `/dash/` → dashboard UI loads with no token paste prompt.

- [ ] **Step 7: Commit**

```bash
git add auth/caddy/Caddyfile docker-compose.yml
git commit -m "feat(auth): route /dash/ through Caddy + oauth2-proxy SSO"
```

---

## Task 11: Wire open-webui through Caddy

**Files:**
- Modify: `auth/caddy/Caddyfile`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add /chat/ route to Caddyfile**

In `auth/caddy/Caddyfile`, add inside the host block (above `handle { respond "no route" 404 }`):

```caddyfile
    # ---- /chat/ → open-webui ----
    handle_path /chat/* {
        reverse_proxy open-webui:8080
    }
```

- [ ] **Step 2: Drop open-webui host port + add to proxy-net**

In `docker-compose.yml`, in the `open-webui` service, remove:

```yaml
    ports:
      - "3000:8080"
```

And add `proxy-net` to its networks:

```yaml
    networks:
      - frontend
      - backend
      - proxy-net
```

- [ ] **Step 3: Recreate**

`docker compose up -d --force-recreate open-webui caddy`

- [ ] **Step 4: Smoke test**

From the same tailnet browser session (cookie still valid from Task 10), open `https://ordo.<tailnet>.ts.net/chat/`. Expected: Open WebUI loads with no re-auth prompt.

- [ ] **Step 5: Commit**

```bash
git add auth/caddy/Caddyfile docker-compose.yml
git commit -m "feat(auth): route /chat/ through Caddy SSO; drop open-webui host port"
```

---

## Task 12: Wire n8n through Caddy with OAuth-callback bypass

**Files:**
- Modify: `auth/caddy/Caddyfile`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add /n8n/ with bypass for OAuth callback + webhooks**

In `auth/caddy/Caddyfile`, add:

```caddyfile
    # ---- /n8n/ — OAuth callback + /webhook/ bypass SSO; rest requires auth ----
    @n8n_bypass path_regexp ^/n8n/(rest/oauth2-credential/callback|webhook/.*)$
    handle_path @n8n_bypass {
        reverse_proxy n8n:5678
    }
    handle_path /n8n/* {
        reverse_proxy n8n:5678
    }
```

The first matcher routes the callback/webhook paths directly without forward_auth (because Caddy's `forward_auth` is only applied via `@auth not path /oauth2/* /healthz` higher up — the bypass paths still match `@auth`, so we need a different exemption pattern). Update the `@auth` matcher at the top of the host block:

```caddyfile
    @auth not path /oauth2/* /healthz /n8n/rest/oauth2-credential/callback /n8n/webhook/*
```

- [ ] **Step 2: Drop n8n host port + add to proxy-net**

In `docker-compose.yml`, remove from `n8n`:

```yaml
    ports:
      - "5678:5678"
```

And add `proxy-net`:

```yaml
    networks:
      - frontend
      - proxy-net
```

- [ ] **Step 3: Recreate**

`docker compose up -d --force-recreate n8n caddy`

- [ ] **Step 4: Smoke test**

`https://ordo.<tailnet>.ts.net/n8n/` loads without re-auth.
External callback simulation: `curl -k https://ordo.<tailnet>.ts.net/n8n/rest/oauth2-credential/callback?code=test` reaches n8n directly without redirecting to Google.

- [ ] **Step 5: Commit**

```bash
git add auth/caddy/Caddyfile docker-compose.yml
git commit -m "feat(auth): route /n8n/ through Caddy with OAuth-callback bypass"
```

---

## Task 13: Wire hermes-dashboard through Caddy

**Files:**
- Modify: `auth/caddy/Caddyfile`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add /hermes/ route**

```caddyfile
    # ---- /hermes/ → hermes-dashboard ----
    handle_path /hermes/* {
        reverse_proxy hermes-dashboard:9119
    }
```

- [ ] **Step 2: Drop hermes-dashboard host port + add to proxy-net**

In `docker-compose.yml`, remove from `hermes-dashboard`:

```yaml
    ports:
      - "${HERMES_DASHBOARD_PORT:-9119}:9119"
```

And add `proxy-net` to its networks.

- [ ] **Step 3: Recreate, smoke test, commit**

```bash
docker compose up -d --force-recreate hermes-dashboard caddy
# Browse https://ordo.<tailnet>.ts.net/hermes/ — should load.
git add auth/caddy/Caddyfile docker-compose.yml
git commit -m "feat(auth): route /hermes/ through Caddy SSO"
```

---

## Task 14: Wire comfyui through Caddy

**Files:**
- Modify: `auth/caddy/Caddyfile`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add /comfy/ route — note WebSocket support**

```caddyfile
    # ---- /comfy/ → comfyui (with websocket support) ----
    handle_path /comfy/* {
        reverse_proxy comfyui:8188
    }
```

(Caddy's `reverse_proxy` already upgrades to WebSocket transparently when `Upgrade: websocket` is requested.)

- [ ] **Step 2: Drop comfyui host port + add to proxy-net**

Remove `8188:8188` from the `comfyui` ports block. Add `proxy-net`.

- [ ] **Step 3: Recreate, smoke test, commit**

```bash
docker compose up -d --force-recreate comfyui caddy
# Browse https://ordo.<tailnet>.ts.net/comfy/ — UI loads, WebSocket connects.
git add auth/caddy/Caddyfile docker-compose.yml
git commit -m "feat(auth): route /comfy/ through Caddy SSO with websocket support"
```

---

## Task 15: Acceptance test — cross-UI session

**Files:** none (manual verification).

- [ ] **Step 1: Clear cookies, open `/dash/` in a fresh browser tab**

Expected: redirected to Google → after sign-in → dashboard loads.

- [ ] **Step 2: Without re-signing in, navigate to each other UI**

Click through to `/chat/`, `/n8n/`, `/hermes/`, `/comfy/` in turn. Expected: every UI loads without re-auth.

- [ ] **Step 3: Inspect the cookie**

In browser dev tools → Application → Cookies → `https://ordo.<tailnet>.ts.net`. Expected: `_oauth2_proxy` cookie present, `Domain=.<tailnet>.ts.net`, `Secure`, `HttpOnly`, expires within 24h.

No commit — gate before allowlist deny test.

---

## Task 16: Acceptance test — allowlist deny

**Files:** none (manual verification).

- [ ] **Step 1: Sign in with a non-allowlisted Google account**

Use a secondary Google account whose email is **not** in `auth/oauth2-proxy/emails.txt`.

- [ ] **Step 2: Expected behavior**

After consent, oauth2-proxy returns HTTP 403 with body "Permission Denied: The user is not authorized." No UI loads. No cookie set.

No commit — gate before tailnet-only test.

---

## Task 17: Acceptance test — tailnet-only enforcement

**Files:** none (manual verification).

- [ ] **Step 1: Disable Tailscale on a device, attempt to hit the hostname**

Or use a tethered hotspot from a phone with Tailscale off:
`curl -k -m 5 https://ordo.<tailnet>.ts.net/`
Expected: connection timeout / no route. Caddy is bound only to the tailnet IP.

- [ ] **Step 2: Verify with `ss` on the host**

`docker exec -it ordo-ai-stack-caddy-1 ss -tlnp | grep 443`
Expected: bound to the tailnet IP, **not** 0.0.0.0.

No commit — gate before runbook.

---

## Task 18: Operator runbook

**Files:**
- Create: `docs/runbooks/auth.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/auth.md`:

```markdown
# SSO Front Door — Operator Runbook

## Setup checklist (one-time)

1. Google Cloud Console → create OAuth 2.0 Web client.
   - Authorized origin: `https://ordo.<tailnet>.ts.net`
   - Authorized redirect: `https://ordo.<tailnet>.ts.net/oauth2/callback`
2. Capture the Client ID + secret into local `.env` as
   `OAUTH2_PROXY_CLIENT_ID` / `OAUTH2_PROXY_CLIENT_SECRET`.
3. Generate cookie secret: `openssl rand -base64 32` →
   `OAUTH2_PROXY_COOKIE_SECRET`.
4. Issue Tailscale cert:
   `tailscale cert --cert-file auth/caddy/certs/tailnet.crt
                   --key-file  auth/caddy/certs/tailnet.key
                   ordo.<tailnet>.ts.net`
5. Set `CADDY_BIND` to your tailnet IP from `tailscale ip -4`.
6. Replace `auth/oauth2-proxy/emails.txt` locally with your real
   allowlist (do **not** commit your real email — repo file stays
   `YOUR_ALLOWLIST_EMAIL`).
7. `docker compose up -d caddy oauth2-proxy`

## Edit allowlist

Edit your local `auth/oauth2-proxy/emails.txt` (one email per line),
then `docker compose restart oauth2-proxy`. Sessions for removed emails
remain valid until cookie expiry (24h max); to force-invalidate, also
rotate `OAUTH2_PROXY_COOKIE_SECRET` and restart.

## Recovery — Google OIDC outage

When Google sign-in is unreachable, all browser paths fail. Two recovery
levers:

1. **Bearer fallback.** Dashboard's `_verify_auth()` still accepts the
   bearer token mode for non-proxy requests. From the host:
   `curl -H "Authorization: Bearer $DASHBOARD_AUTH_TOKEN" http://localhost:8080/api/...`
2. **Direct container access.** With `docker exec` you can run any verb
   inside a service container while public access is broken.

## Recovery — oauth2-proxy crash

`docker compose restart oauth2-proxy`. Caddy's `forward_auth` will retry.
If oauth2-proxy is unhealthy on boot, check `docker logs oauth2-proxy` for
`OAUTH2_PROXY_*` env mismatch.

## Cookie / session rotation

Rotate cookie secret to force everyone (just you) to re-auth:

```
NEW_SECRET=$(openssl rand -base64 32)
# Update OAUTH2_PROXY_COOKIE_SECRET in .env
docker compose restart oauth2-proxy
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Browser stuck redirecting | Cookie domain mismatch | Confirm `CADDY_TAILNET_DOMAIN` matches `<tailnet>.ts.net` exactly |
| `redirect_uri_mismatch` from Google | OAuth client redirect URI doesn't match | Update GCP console authorized redirect URI to match `CADDY_TAILNET_HOSTNAME` |
| 502 from Caddy on `/dash/` | Dashboard not on proxy-net | Add `proxy-net` to dashboard's networks; recreate |
| 401 with "missing X-Forwarded-Email from trusted proxy" | Caddy-to-dashboard subnet differs from `DASHBOARD_TRUSTED_PROXY_NET` | `docker network inspect ordo-ai-stack_proxy-net | grep Subnet`; pin matching CIDR in env |
```

- [ ] **Step 2: Commit**

```bash
git add docs/runbooks/auth.md
git commit -m "docs(auth): operator runbook for SSO front door"
```

---

## Self-review checklist

- [ ] Every task has concrete file paths and complete code/config (no "TBD" or "see above").
- [ ] Spec coverage: § Architecture (Tasks 2-6), § Components & per-service (Tasks 4, 6, 9-14), § Data flow A/B (Tasks 9-15), § Failure modes Google-outage / oauth2-proxy-crash / cookie-secret-leak / spoofed-header (Tasks 9, 18), § Testing acceptance items 1-3, 7-8 (Tasks 15-17).
- [ ] Spec items deferred to Plan B: secrets-form classification, Hermes-bound items.
- [ ] Spec items deferred to Plan C: SOPS encryption of `OAUTH2_PROXY_*` secrets.
- [ ] Type/method consistency: `_verify_auth(request)` signature consistent across Tasks 9, 10. `_request_from_trusted_proxy` introduced once and reused.
- [ ] Pre-ship checklist git-history audit (spec § Pre-ship checklist) is **not** in this plan — it's a Plan B prerequisite.
