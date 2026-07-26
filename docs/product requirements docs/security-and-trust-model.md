# Security & Trust Model

## Threat Model

| Asset | Threat | Current State | Mitigation |
|-------|--------|---------------|------------|
| `docker.sock` (ops-api) | Container escape → host RCE | Mounted; allowlisted actions only | Bearer-token auth; no host port; allowlist in code; every privileged call audited. (The `ordo serve` scheduler on ops-controller mounts a *separate* `<project>-*`-scoped docker.sock for render/broker lifecycle only — auth-free by design, exposes no start/stop/logs/pull API.) |
| `docker.sock` (mcp-gateway) | MCP server escapes → host pivot | Mounted; Docker MCP Gateway owns it | Accept: required for spawning server containers; mcp-gateway sits on the single `ordo-net` and publishes no host port (reached only via Caddy `:443/mcp`, bearer token) |
| Ops controller token | Token theft → privileged ops | Token in `out/secrets.env`; no default | Generate with `openssl rand -hex 32`; never expose controller port to host |
| MCP tools (filesystem) | Data exfiltration via tool | Enabled in servers.txt; broken without root-dir | Remove from default servers.txt; require explicit opt-in |
| MCP tools (browser/playwright) | SSRF → RFC1918/metadata | No egress blocks yet | Add `DOCKER-USER` iptables egress block; document in runbooks |
| Tool output → model | Prompt injection via tool output | No sandbox; tool output passed to model | Allowlists; structured tool calls (`<tool_result>` tags); validate tool schemas |
| Dashboard auth | Unauthenticated admin | Gated by the Caddy edge (oauth2-proxy + Google SSO + email allowlist) on its dedicated SSO-gated port under the port-per-service model (`:8444`, plus `/grafana/` embed); the dashboard container itself publishes no host port and is reached only via that Caddy port or the internal `ordo-net` for service-to-service calls. App code retains an optional, dormant `DASHBOARD_AUTH_TOKEN` Bearer fallback, unused in this deployment | Edge SSO is the auth boundary for the dashboard; no per-service token to manage |
| WEBUI_AUTH=False | Open WebUI accessible without auth | Explicit in compose env | Change default to `WEBUI_AUTH=${WEBUI_AUTH:-True}`; opt-out, not opt-in |
| Model gateway | No auth on `/v1/` endpoints | None; local-first intentional | Acceptable for localhost; add API key support if exposed to LAN |

## AuthN / AuthZ Tiers

- **Tier 0:** No auth (health endpoints, read-only model list)
- **Tier 1:** Bearer token (ops-api — `OPS_CONTROLLER_TOKEN`)
- **Tier 2:** Edge SSO (Caddy oauth2-proxy + Google SSO + email allowlist) — the sole auth gate for every UI, including the dashboard. Caddy is the *only* service publishing host ports: under the port-per-service model, one shared Google sign-in (domain-scoped cookie, one OAuth callback) covers seven SSO-gated ports on `${CADDY_TAILNET_HOSTNAME}` — `:443` front door (landing page, `/oauth2` callback, `/llm/*` and `/mcp` Bearer-token APIs, n8n webhook/OAuth passthroughs, and 302s from every legacy subpath) plus one dedicated port per UI: `:8443` Open WebUI, `:8444` Dashboard (+ `/grafana/` embed), `:8445` n8n, `:8446` ComfyUI, `:8447` Hermes, `:8448` codebase-memory. UI service containers themselves have no host port and are reached only through their Caddy port or the internal `ordo-net`. The dashboard has no per-service auth token in this deployment (`DASHBOARD_AUTH_TOKEN` unset, `AUTH_REQUIRED=False`); the app code's optional Bearer fallback is dormant
- **Future Tier 3:** Per-role OIDC / RBAC beyond the edge's binary allow/deny gate (if deeper multi-user separation is needed)
- **RBAC:** Currently binary (authed = full access). Future: read-only role (view logs, health) vs admin role (start/stop).

## Correlation ID Flow

1. External client sends `X-Request-ID: req-abc` to model gateway
2. Model gateway logs it; includes in throughput record to dashboard
3. Dashboard passes `X-Request-ID` when calling ops-api
4. Ops-api includes in audit entry
5. Result: one request traceable across model → throughput → ops → audit

## Secret Handling

### End-to-End

- `out/secrets.env` — gitignored, host-only, rendered from `out/secrets.env.example`; not committed
- MCP tool secrets (e.g. `GITHUB_PERSONAL_ACCESS_TOKEN`, `N8N_API_KEY`, `MCP_GATEWAY_TOKEN`) — same `out/secrets.env`, consumed by `mcp-gateway` via the rendered compose `env_file:`
- Agent runtime state under `data/hermes/` — gitignored; Discord bot token is supplied via Docker secrets (file at `/run/secrets/discord_token`, SOPS-encrypted at rest under `secrets/discord_token.sops`); per-user allowlists are runtime state inside `data/hermes/`.
- Gateway tokens — in `out/secrets.env`, set via compose `env_file:`
- **Secret rotation:** Update `out/secrets.env` (or re-render via `ordo render`), then `docker compose -p ordo up -d --force-recreate <service>` from `out/`.

### Stack Secrets

| Secret | Location | Injected by | Notes |
|--------|----------|-------------|-------|
| `OPS_CONTROLLER_TOKEN` | `out/secrets.env` | Compose `env_file:` (`secrets.env`) | Required for the ops-api privileged (Bearer) API |
| `DISCORD_BOT_TOKEN` | `secrets/discord_token.sops` | Docker secret → hermes-gateway (`/run/secrets/discord_token`) | Optional, only when Discord channel is used |
| `HF_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN` | `out/secrets.env` | Compose `env_file:` (`secrets.env`) | Optional, for gated HF model pulls and GitHub MCP |

## SSRF Defenses (MCP)

```bash
# Block MCP containers from reaching RFC1918 + metadata endpoints
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 10.0.0.0/8 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 172.16.0.0/12 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 192.168.0.0/16 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 100.64.0.0/10 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 169.254.169.254/32 -j DROP
```

SSRF scripts live at `scripts/ssrf-egress-block.sh` (Linux/WSL2) and `scripts/ssrf-egress-block.ps1` (Windows guidance).

### Browser-Tier Egress Control

When browser/playwright is active, worker containers can make arbitrary outbound HTTP requests. Apply RFC1918 + metadata blocks:

```bash
./scripts/ssrf-egress-block.sh
```

(No `--target` flag exists. The script auto-detects the `ordo-net` subnet;
pass `--dry-run` to preview, `--remove` to undo, or an explicit
`SUBNET` argument, e.g. `172.18.0.0/16`, to override detection.)

Blocked ranges: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` (RFC1918), `100.64.0.0/10` (Tailscale CGNAT), `169.254.169.254/32`, `169.254.170.2/32` (cloud metadata). DNS (port 53) is explicitly allowed.

## Prompt Injection Defense at Tool-Output Boundary

- Tool results returned in structured boundaries by the MCP gateway and agent
- Agents treat tool output as **data**, not **instructions**
- Validate tool output schemas where possible (MCP `registry-custom.yaml` `outputSchema` field)
- Structured boundaries help the model distinguish injected text from genuine prompts

## Container Hardening

Custom services (model-gateway, dashboard, ops-controller, hermes-gateway, hermes-dashboard, mcp-gateway, orchestration-mcp, comfyui-mcp, rag-ingestion, worker) run with:

```yaml
cap_drop: [ALL]
security_opt: ["no-new-privileges:true"]
```

Resource limits, healthchecks, and `restart: unless-stopped` are applied per-service in the rendered `out/docker-compose.yml` (regenerated by `ordo render`, never hand-edited). One-shot containers (pullers, setup scripts) use `restart: "no"`.

## Security + Reliability Intersection

Items that are both security and reliability problems:
- Open WebUI auth default
- MCP per-client enforcement gaps

**Improvements:** auth on by default for remotely reachable UIs; env-based secret resolution where possible; explicit per-client policy at MCP gateway; tool registration workflow; immutable audit trail for config changes.
