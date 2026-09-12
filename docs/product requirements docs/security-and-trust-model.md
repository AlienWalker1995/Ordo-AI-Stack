# Security & Trust Model

## Threat Model

| Asset | Threat | Current State | Mitigation |
|-------|--------|---------------|------------|
| `docker.sock` (ops-api) | Container escape → host RCE | Mounted; allowlisted actions only | Bearer-token auth; no host port; allowlist in code; every privileged call audited. (The `ordo serve` scheduler on ops-controller mounts a *separate* `<project>-*`-scoped docker.sock for render/broker lifecycle only — auth-free by design, exposes no start/stop/logs/pull API.) |
| MCP server containers | MCP server compromise → lateral movement | No Docker socket anywhere in the tool path; each server is a long-lived service on `ordo-mcp-net` (`internal: true`) | `no-new-privileges`, 1 CPU / 2 GB, no `env_file` (only its own declared env and secrets), reachable only by `model-gateway`, no host port |
| Ops controller token | Token theft → privileged ops | Token in `out/secrets.env`; no default | Generate with `openssl rand -hex 32`; never expose controller port to host |
| MCP tools (filesystem) | Data exfiltration via tool | Enabled as a `kind: mcp` plugin in `ordo.yaml`; each declares its own mounts (code root read-only) | Drop the plugin from `plugins:`, `ordo render`, recreate `model-gateway`; grant it to no key otherwise |
| MCP tools with egress (`searxng`) | SSRF → RFC1918/metadata | Only servers declaring `network: stack` reach anything beyond `ordo-mcp-net` | Add `DOCKER-USER` iptables egress block; document in runbooks |
| Tool output → model | Prompt injection via tool output | No sandbox; tool output passed to model | Allowlists; structured tool calls (`<tool_result>` tags); validate tool schemas |
| Dashboard auth | Unauthenticated admin | Gated by the Caddy edge (oauth2-proxy + Google SSO + email allowlist) on its dedicated SSO-gated port under the port-per-service model (`:8444`, plus `/grafana/` embed); the dashboard container itself publishes no host port and is reached only via that Caddy port or the internal `ordo-net` for service-to-service calls. App code retains an optional, dormant `DASHBOARD_AUTH_TOKEN` Bearer fallback, unused in this deployment | Edge SSO is the auth boundary for the dashboard; no per-service token to manage |
| WEBUI_AUTH=False | Open WebUI accessible without auth | Explicit in compose env | Change default to `WEBUI_AUTH=${WEBUI_AUTH:-True}`; opt-out, not opt-in |
| Model gateway | Unauthorized model or tool access | Master key enforced at startup (`^sk-[A-Za-z0-9_-]{32,}$`, `local` can never run again); per-consumer virtual keys with model and MCP grants | Keys in `out/secrets.env`; missing or invalid key → 401 on `/v1/*` and `/mcp` |

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
- MCP tool secrets (e.g. `N8N_API_KEY`) — same `out/secrets.env`, but an `mcp-*` service has **no** `env_file`: only the keys its manifest declares in `env:` / `secrets:` are interpolated into its compose environment
- Agent runtime state under `data/hermes/` — gitignored; Discord bot token is supplied via Docker secrets (file at `/run/secrets/discord_token`, SOPS-encrypted at rest under `secrets/discord_token.sops`); per-user allowlists are runtime state inside `data/hermes/`.
- Gateway tokens — in `out/secrets.env`, set via compose `env_file:`
- **Secret rotation:** Update `out/secrets.env` (or re-render via `ordo render`), then `docker compose -p ordo up -d --force-recreate <service>` from `out/`.

### Stack Secrets

| Secret | Location | Injected by | Notes |
|--------|----------|-------------|-------|
| `LITELLM_MASTER_KEY` | `out/secrets.env` | Compose `env_file:` (`secrets.env`) | `sk-` + 32+ chars, enforced by the entrypoint; LiteLLM admin UI login |
| `LITELLM_SALT_KEY` | `out/secrets.env` | Compose `env_file:` (`secrets.env`) | Encrypts provider credentials in `litellm-db`. **Never rotate** (excluded from `rotate-internal.sh`) |
| `LITELLM_DB_PASSWORD` | `out/secrets.env` | Compose `env_file:` (`secrets.env`) | Postgres password for `litellm-db` |
| `LITELLM_KEY_HERMES` / `_OPEN_WEBUI` / `_AUTOMATION` / `_EDGE` | `out/secrets.env` | Compose `env_file:` / per-service `environment:` | Per-consumer virtual keys, provisioned by `model-gateway-keys` |
| `OPS_CONTROLLER_TOKEN` | `out/secrets.env` | Compose `env_file:` (`secrets.env`) | Required for the ops-api privileged (Bearer) API |
| `DISCORD_BOT_TOKEN` | `secrets/discord_token.sops` | Docker secret → hermes-gateway (`/run/secrets/discord_token`) | Optional, only when Discord channel is used |
| `HF_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN` | `out/secrets.env` | Compose `env_file:` (`secrets.env`) | Optional, for gated HF model pulls and ComfyUI-Manager custom-node fetches |

## SSRF Defenses (MCP)

```bash
# Block MCP containers from reaching RFC1918 + metadata endpoints
iptables -I DOCKER-USER -s <ordo_mcp_net_subnet> -d 10.0.0.0/8 -j DROP
iptables -I DOCKER-USER -s <ordo_mcp_net_subnet> -d 172.16.0.0/12 -j DROP
iptables -I DOCKER-USER -s <ordo_mcp_net_subnet> -d 192.168.0.0/16 -j DROP
iptables -I DOCKER-USER -s <ordo_mcp_net_subnet> -d 100.64.0.0/10 -j DROP
iptables -I DOCKER-USER -s <ordo_mcp_net_subnet> -d 169.254.169.254/32 -j DROP
```

A server declaring `network: internal` has no route off `ordo-mcp-net` at all, so these rules only matter for servers declaring `network: stack`.

SSRF scripts live at `scripts/ssrf-egress-block.sh` (Linux/WSL2) and `scripts/ssrf-egress-block.ps1` (Windows guidance).

### Browser-Tier Egress Control

When an MCP server declares `network: stack` (today only `searxng`), it can make outbound HTTP requests. Apply RFC1918 + metadata blocks:

```bash
./scripts/ssrf-egress-block.sh
```

(No `--target` flag exists. The script auto-detects the `ordo-net` subnet;
pass `--dry-run` to preview, `--remove` to undo, or an explicit
`SUBNET` argument, e.g. `172.18.0.0/16`, to override detection.)

Blocked ranges: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` (RFC1918), `100.64.0.0/10` (Tailscale CGNAT), `169.254.169.254/32`, `169.254.170.2/32` (cloud metadata). DNS (port 53) is explicitly allowed.

## Prompt Injection Defense at Tool-Output Boundary

- Tool results returned in structured boundaries by the gateway's MCP endpoint and the agent
- Agents treat tool output as **data**, not **instructions**
- Validate tool output schemas where possible (the schemas each MCP server returns from `tools/list`)
- Structured boundaries help the model distinguish injected text from genuine prompts

## Container Hardening

Custom services (model-gateway, model-gateway-keys, dashboard, ops-controller, hermes-gateway, hermes-dashboard, the `mcp-*` servers, rag-ingestion) run with:

```yaml
cap_drop: [ALL]
security_opt: ["no-new-privileges:true"]
```

Resource limits, healthchecks, and `restart: unless-stopped` are applied per-service in the rendered `out/docker-compose.yml` (regenerated by `ordo render`, never hand-edited). One-shot containers (pullers, setup scripts) use `restart: "no"`.

## Security + Reliability Intersection

Items that are both security and reliability problems:
- Open WebUI auth default
- Per-tool `allowed_tools` narrowing not yet used (per-consumer server scoping is enforced)

**Improvements:** auth on by default for remotely reachable UIs; env-based secret resolution where possible; per-tool `allowed_tools` narrowing (per-consumer server scoping already ships via LiteLLM virtual keys); tool registration workflow; immutable audit trail for config changes.
