# Security & Trust Model

## Threat Model

| Asset | Threat | Current State | Mitigation |
|-------|--------|---------------|------------|
| `docker.sock` (ops-controller, Hermes agent) | Container escape → host RCE | Mounted | ops-controller: no host port; the `DockerBackend` guard scopes every call to `<project>-*` containers; destructive verbs require `confirm: true`; every state-changing call audited, refusals included. Hermes: guardrails in its prompt, see [Hermes owns Docker](../design/hermes-owns-docker.md) |
| MCP server containers | MCP server compromise → lateral movement | No Docker socket anywhere in the tool path; each server is a long-lived service on `ordo-mcp-net` (`internal: true`) | `no-new-privileges`, 1 CPU / 2 GB, no `env_file` (only its own declared env and secrets), reachable only by `model-gateway`, no host port |
| Ops controller token | Token theft → privileged ops | Token in `out/secrets.env`; no default | Generate with `openssl rand -hex 32`; never expose controller port to host |
| MCP tools (filesystem) | Data exfiltration via tool | Enabled as a `kind: mcp` plugin in `ordo.yaml`; each declares its own mounts (code root read-only) | Drop the plugin from `plugins:`, `ordo render`, recreate `model-gateway`; grant it to no key otherwise |
| MCP tools with egress (`searxng`) | SSRF → RFC1918/metadata | Only servers declaring `network: stack` reach anything beyond `ordo-mcp-net` | Add `DOCKER-USER` iptables egress block; document in runbooks |
| Tool output → model | Prompt injection via tool output | No sandbox; tool output passed to model | Allowlists; structured tool calls (`<tool_result>` tags); validate tool schemas |
| Dashboard auth | Unauthenticated admin | Gated by the Caddy edge (oauth2-proxy + Google SSO + email allowlist) on its dedicated SSO-gated port under the port-per-service model (`:8444`, plus `/grafana/` embed); the dashboard container itself publishes no host port. Its state-changing and ops-forwarding routes need the edge SSO identity (trusted only from the `caddy` peer) or the `OPS_CONTROLLER_TOKEN` bearer, so other `ordo-net` containers cannot borrow its ops-controller token | Edge SSO for operators; internal callers reuse the ops-controller bearer, no dashboard-specific token |
| WEBUI_AUTH=False | Open WebUI accessible without auth | Explicit in compose env | Change default to `WEBUI_AUTH=${WEBUI_AUTH:-True}`; opt-out, not opt-in |
| Model gateway | Unauthorized model or tool access | Master key enforced at startup (`^sk-[A-Za-z0-9_-]{32,}$`, `local` can never run again); per-consumer virtual keys with model and MCP grants | Keys in `out/secrets.env`; missing or invalid key → 401 on `/v1/*` and `/mcp` |

## AuthN / AuthZ Tiers

- **Tier 0:** No auth (health endpoints, read-only model list)
- **Tier 1:** Bearer token (`OPS_CONTROLLER_TOKEN`), required on every ops-controller call except the health probe and verified in constant time; it also has no host port
- **Tier 2:** Edge SSO (Caddy oauth2-proxy + Google SSO + email allowlist) — the sign-in for every UI, including the dashboard. Caddy is the *only* service publishing host ports: under the port-per-service model, one shared Google sign-in (domain-scoped cookie, one OAuth callback) covers nine SSO-gated ports on `${CADDY_TAILNET_HOSTNAME}` — `:443` front door (landing page, `/oauth2` callback, `/llm/*` and `/mcp` Bearer-token APIs, n8n webhook/OAuth passthroughs, and 302s from every legacy subpath) plus one dedicated port per UI: `:8443` Open WebUI, `:8444` Dashboard (+ `/grafana/` embed), `:8445` n8n, `:8446` ComfyUI, `:8447` Hermes, `:8448` codebase-memory, `:8449` LiteLLM admin UI, `:8450` Langfuse. UI service containers themselves have no host port and are reached only through their Caddy port or the internal `ordo-net`. The dashboard has no token of its own: its protected routes accept the SSO identity Caddy forwards (from the `caddy` peer only) or the `OPS_CONTROLLER_TOKEN` bearer
- **Future Tier 3:** Per-role OIDC / RBAC beyond the edge's binary allow/deny gate (if deeper multi-user separation is needed)
- **RBAC:** Currently binary (authed = full access). Future: read-only role (view logs, health) vs admin role (start/stop).

## Correlation ID Flow

1. A client sends `X-Request-ID` to the dashboard
2. The dashboard forwards it on its ops-controller calls (`services/dashboard/dashboard/app.py`, `routes_orchestration.py`)
3. Gap: ops-controller does not record it in the audit entry yet

## Secret Handling

### End-to-End

- `out/secrets.env`: gitignored, host-only, materialized by `ordo secrets materialize` from the one secret store (a SOPS file in a private repo, `site: SECRETS_SOURCE`); not committed
- Scoped delivery: no service loads `out/secrets.env` as an `env_file`. Each service's manifest lists the secret names it reads (`secrets:`), the renderer emits `KEY: ${KEY}` into that service's `environment:`, and compose interpolates the values from `--env-file secrets.env` (which `ordo up` / `ordo recreate` and ops-controller always pass). A service holds only the secrets it reads; `tests/substrate/test_secret_scoping.py` pins the contract.
- MCP tool secrets (e.g. `N8N_API_KEY`): same `out/secrets.env`, same scoped interpolation
- Agent runtime state under `data/hermes/`: gitignored; Discord bot token is supplied as a file secret (`/run/secrets/discord_token`, materialized from the secret store into `out/secrets/discord_token`); per-user allowlists are runtime state inside `data/hermes/`.
- Gateway tokens: in `out/secrets.env`, interpolated per service as above
- **Secret rotation:** `ordo secrets rotate KEY...` (or `set KEY --from-stdin` for an issued token) writes the store and materializes `out/secrets.env`, then prints the `ordo recreate --reading KEY...` that recreates every reader (a `restart` keeps the old environment). See `docs/runbooks/secrets.md`.

### Stack Secrets

| Secret | Location | Injected by | Notes |
|--------|----------|-------------|-------|
| `LITELLM_MASTER_KEY` | `out/secrets.env` | Per-service `KEY: ${KEY}`, interpolated from `secrets.env` | `sk-` + 32+ chars, enforced by the entrypoint; LiteLLM admin UI login |
| `LITELLM_SALT_KEY` | `out/secrets.env` | Per-service `KEY: ${KEY}`, interpolated from `secrets.env` | Encrypts provider credentials in `litellm-db`. **Never rotate** (`ordo secrets rotate` refuses it) |
| `LITELLM_DB_PASSWORD` | `out/secrets.env` | Per-service `KEY: ${KEY}`, interpolated from `secrets.env` | Postgres password for `litellm-db` |
| `LITELLM_KEY_HERMES` / `_OPEN_WEBUI` / `_AUTOMATION` / `_EDGE` | `out/secrets.env` | Per-service `KEY: ${KEY}`, interpolated from `secrets.env` | Per-consumer virtual keys, provisioned by `model-gateway-keys` |
| `OPS_CONTROLLER_TOKEN` | `out/secrets.env` | Per-service `KEY: ${KEY}`, interpolated from `secrets.env` | Bearer the dashboard, agent and MCP clients send to ops-controller |
| `DISCORD_BOT_TOKEN` | `secrets/discord_token.sops` | Docker secret → agent (`/run/secrets/discord_token`) | Optional, only when Discord channel is used |
| `HF_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN` | `out/secrets.env` | Per-service `KEY: ${KEY}`, interpolated from `secrets.env` | Optional, for gated HF model pulls and ComfyUI-Manager custom-node fetches |

## SSRF Defenses (MCP)

```bash
# What scripts/ssrf-egress-block.sh inserts, for <subnet> = the ordo-net subnet
# (auto-detected) or an explicit SUBNET argument
iptables -I DOCKER-USER -s <subnet> -d 10.0.0.0/8 -j DROP
iptables -I DOCKER-USER -s <subnet> -d 172.16.0.0/12 -j DROP
iptables -I DOCKER-USER -s <subnet> -d 192.168.0.0/16 -j DROP
iptables -I DOCKER-USER -s <subnet> -d 100.64.0.0/10 -j DROP
iptables -I DOCKER-USER -s <subnet> -d 169.254.169.254/32 -j DROP
iptables -I DOCKER-USER -s <subnet> -d 169.254.170.2/32 -j DROP
iptables -I DOCKER-USER -s <subnet> -p udp --dport 53 -j ACCEPT   # DNS stays open
iptables -I DOCKER-USER -s <subnet> -p tcp --dport 53 -j ACCEPT
```

A server declaring `network: internal` has no route off `ordo-mcp-net` at all (`internal: true`), so a rule scoped to that network would be a no-op; the servers these rules exist for are the ones declaring `network: stack`, which egress via `ordo-net`. That is why the script targets `ordo-net`.

> **Warning:** `ordo-net` is the whole stack network. With the default target the rules apply to every container on it, not only the MCP servers, so they also cut the agent's LAN and tailnet access. Scoping them to MCP servers alone would need a dedicated egress network (or per-container source IPs) for `network: stack` servers, which the stack does not have today.

SSRF scripts live at `scripts/ssrf-egress-block.sh` (Linux/WSL2) and `scripts/ssrf-egress-block.ps1` (Windows guidance).

### Browser-Tier Egress Control

When an MCP server declares `network: stack` (for example `searxng`), it can make outbound HTTP requests. Apply RFC1918 + metadata blocks:

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

Custom services (model-gateway, model-gateway-keys, dashboard, ops-controller, agent, hermes-dashboard, the `mcp-*` servers, rag-ingestion) run with:

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
