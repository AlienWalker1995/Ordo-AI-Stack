# OpenClaw Security & Trust Boundary

See [docs/ARCHITECTURE_RFC.md](../docs/ARCHITECTURE_RFC.md) Section 9 for the full orchestrator/browser
trust model. This document is the operational quick-reference.

---

## Trust Model Overview

OpenClaw uses a **two-tier trust model** that mirrors Anthropic's agent safety guidance:

- **Orchestrator** (`openclaw-gateway`): holds all credentials, directs all work
- **Browser-tier worker** (`openclaw-cli`, future `openclaw-browser`): holds no session credentials, limited access

```
[orchestrator]  openclaw-gateway
                  ├── CLAUDE_AI_SESSION_KEY    ← session credentials (orchestrator ONLY)
                  ├── CLAUDE_WEB_SESSION_KEY
                  ├── CLAUDE_WEB_COOKIE
                  ├── OPENCLAW_GATEWAY_TOKEN
                  ├── /home/node/.openclaw     (read-write config + secrets)
                  └── /home/node/.openclaw/workspace (read-write)

[browser-tier]  openclaw-cli
                  ├── OPENCLAW_GATEWAY_TOKEN   ← gateway bridge token ONLY
                  └── /home/node/.openclaw/workspace (READ-ONLY)
                  ✗ no session credentials
                  ✗ no openclaw.json access
```

**Core invariants:**
1. Session credentials (`CLAUDE_*`) never appear in any browser-tier container environment.
2. `openclaw.json` (Telegram tokens, skill API keys) is never mounted in browser-tier containers.
3. Workspace is read-only in browser-tier containers.
4. Egress from browser-tier containers is blocked to RFC1918 and cloud metadata endpoints.

---

## Quick Hardening Checklist

### 1. Bind UI to localhost only

Set in `.env` or use the secure override:

```bash
# Use the secure override (recommended):
docker compose -f docker-compose.yml -f overrides/openclaw-secure.yml up -d
```

Or manually in `.env`:
```bash
# Bind gateway to localhost only (access via Tailscale Serve)
OPENCLAW_GATEWAY_PORT=127.0.0.1:18789
OPENCLAW_BRIDGE_PORT=127.0.0.1:18790
```

### 2. Tailscale access

- Keep OpenClaw **off the public internet**
- Use **Tailscale Serve** to expose `127.0.0.1:18789` to your tailnet only
- Avoid Funnel unless intentional public exposure

### 3. Browser worker egress (web crawling / playwright)

If OpenClaw uses a browser worker for dynamic sites, block private ranges.

**Apply via script (recommended):**
```bash
# From repo root on a Linux host:
./scripts/ssrf-egress-block.sh --target openclaw   # openclaw network only
./scripts/ssrf-egress-block.sh --target all        # MCP + openclaw (recommended)
```

**Manual iptables (host firewall / DOCKER-USER chain):**
```bash
# Find openclaw subnet:
SUBNET=$(docker network inspect ai-toolkit-openclaw 2>/dev/null \
  | jq -r '.[0].IPAM.Config[0].Subnet // empty')
# Fall back to frontend network if openclaw-specific network not in use:
[ -z "$SUBNET" ] && SUBNET=$(docker network inspect ai-toolkit-frontend \
  | jq -r '.[0].IPAM.Config[0].Subnet // empty')

sudo iptables -I DOCKER-USER -s "$SUBNET" -d 10.0.0.0/8       -j DROP
sudo iptables -I DOCKER-USER -s "$SUBNET" -d 172.16.0.0/12    -j DROP
sudo iptables -I DOCKER-USER -s "$SUBNET" -d 192.168.0.0/16   -j DROP
sudo iptables -I DOCKER-USER -s "$SUBNET" -d 100.64.0.0/10    -j DROP
sudo iptables -I DOCKER-USER -s "$SUBNET" -d 169.254.169.254/32 -j DROP
sudo iptables -I DOCKER-USER -s "$SUBNET" -d 169.254.170.2/32   -j DROP
# Allow DNS (required for external tool calls):
sudo iptables -I DOCKER-USER -s "$SUBNET" -p udp --dport 53   -j ACCEPT
sudo iptables -I DOCKER-USER -s "$SUBNET" -p tcp --dport 53   -j ACCEPT
```

### 4. Secrets

- Session credentials (`CLAUDE_AI_SESSION_KEY`, `CLAUDE_WEB_SESSION_KEY`, `CLAUDE_WEB_COOKIE`) are set only on `openclaw-gateway` via `.env`
- `openclaw-cli` and any browser worker receive **only** `OPENCLAW_GATEWAY_TOKEN`
- `data/openclaw/openclaw.json` (Telegram token, skill API keys) is only mounted in `openclaw-gateway`
- **Do not** include `data/openclaw/` in unencrypted cloud backups

### 5. Container hardening

Both containers ship with:
```yaml
cap_drop: [ALL]
security_opt: ["no-new-privileges:true"]
```

`openclaw-gateway` additionally has a 2 GB memory limit and a healthcheck on `:18789`.

`openclaw-cli` has `restart: "no"` — it is interactive and must not restart automatically.

---

## Adding a Future Browser Worker (`openclaw-browser`)

When OpenClaw gains a dedicated Playwright/browser container, apply this template in an override
compose file:

```yaml
services:
  openclaw-browser:
    # NO environment secrets — zero credentials
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    read_only: true
    tmpfs: ["/tmp", "/home/node/downloads"]
    networks:
      - openclaw-browser   # dedicated egress-blocked network; no backend access
    # Explicit egress block applied via ssrf-egress-block.sh --target openclaw
```

Invariant: the browser container must have **no host mounts** containing secrets, and must be on an
egress-blocked network before any real browsing is enabled.

---

## Threat Mitigations (OpenClaw-specific)

| Threat | Control | Notes |
|--------|---------|-------|
| Session credential theft via CLI compromise | CLI receives no session credentials | Enforced in `docker-compose.yml` |
| Secret exfiltration via config volume | Config dir not mounted in CLI | `openclaw.json` gateway-only |
| SSRF from browser worker to internal services | RFC1918 egress block + metadata block | `ssrf-egress-block.sh --target openclaw` |
| Prompt injection escalating to credential access | Browser-tier has no credentials to exfiltrate | Two-tier model |
| Prompt injection via tool output | Structured `<tool_result>` boundary in MCP bridge | Prevents verbatim interpolation into system prompt |
| Container privilege escalation | `cap_drop: [ALL]` + `no-new-privileges:true` | Both containers |
| Unbounded resource consumption | 2 GB memory limit on gateway | `deploy.resources.limits` |
| Gateway health degradation undetected | Healthcheck on `:18789` (30s interval) | `healthcheck:` in compose |
| Token exposure in unencrypted backup | Gitignored `data/openclaw/` | Document: exclude from cloud backup |

---

## See Also

- [docs/ARCHITECTURE_RFC.md](../docs/ARCHITECTURE_RFC.md) — Section 9 (full trust model) · WS4 (threat model)
- [docs/runbooks/SECURITY_HARDENING.md](../docs/runbooks/SECURITY_HARDENING.md) — §1 SSRF blocks · §5 token rotation · §11 openclaw secrets
- [SECURITY.md](../SECURITY.md) — pre-deployment checklist · break-glass
