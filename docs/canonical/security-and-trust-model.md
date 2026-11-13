---
authority: canonical
status: current
owner: operator
last_verified: 2026-11-12
verified_against: 1c97d8f3608b
---

# Security & Trust Model

## Threat Model

| Asset | Threat | Current State | Mitigation |
|-------|--------|---------------|------------|
| `docker.sock` (ops-controller, Hermes agent) | Container escape → host RCE | Mounted | ops-controller: no host port; the `DockerBackend` guard scopes every call to `<project>-*` containers; destructive verbs require `confirm: true`; every state-changing call audited, refusals included. Hermes: guardrails in its prompt only, which do not hold against injected instructions. Decided: the agent's socket is removed and its Docker work moves to ops-controller, see [ADR-0001: The agent holds no Docker socket](adr-0001-agent-holds-no-docker-socket.md) |
| `docker.sock` via `docker-socket-proxy` (Alloy, the log shipper) | Docker API read beyond logs | Mounted into the proxy only (`:ro`, defence in depth: a read-only bind of a socket still accepts writes through it) | The GET/HEAD path allowlist is the read-only control: it answers 403 to everything but `_ping`, `version`, the container list, each container's inspect and logs, and the network list: no container file reads (`/archive`), exports, image or host info, and no writes. Inspect still returns each container's environment, so the proxy sits on the monitoring plugin's private internal `docker-api` network that only Alloy joins, and `-allowfrom=alloy` refuses any other caller |
| Loki (every container's logs, monitoring plugin) | A peer reads leaked secrets from logs, forges lines or deletes them | No auth (`auth_enabled: false`); Loki 3.x has no admin API | Loki sits only on the plugin's private internal `logs` network: Alloy (the one writer) and Grafana (the one reader) join it, no other service can reach it |
| MCP server containers | MCP server compromise → lateral movement | No Docker socket anywhere in the tool path; each server is a long-lived service on `ordo-mcp-net` (`internal: true`); `network: stack` servers also join `ordo-net` and have full egress (see the next rows) | `no-new-privileges`, 1 CPU / 2 GB, no `env_file` (only its own declared env and secrets), reachable only by `model-gateway`, no host port |
| Ops controller token | Token theft → privileged ops | Token in `out/secrets.env`; no default | Generate with `openssl rand -hex 32`; never expose controller port to host |
| MCP tools (filesystem) | Data exfiltration via tool | A `kind: mcp` plugin (declared in its manifest, enabled in `ordo.yaml`'s `plugins:`); each declares its own mounts. Today only `memory-vault` mounts data: the vault, read-write by design | Drop the plugin from `plugins:`, `ordo render`, recreate `model-gateway`; grant it to no key otherwise |
| MCP tools with egress (`network: stack`: `searxng`, `qdrant-rag`, `orchestration`, `comfyui-mcp`) | SSRF → RFC1918/tailnet/metadata/stack services | Unmitigated on Docker Desktop; on a Linux engine only if the operator runs `ssrf-egress-block.sh` by hand | See [SSRF Defenses (MCP)](#ssrf-defenses-mcp): per-server internal networks, then an egress proxy for `searxng` |
| Tool output → model | Prompt injection via tool output | No sandbox; tool output passed to model | Allowlists; structured tool calls (Hermes wraps MCP results in `<untrusted_tool_result>` with a treat-as-data preamble); validate tool schemas |
| Dashboard auth | Unauthenticated admin | Gated by the Caddy edge (oauth2-proxy + Google SSO + email allowlist) on its dedicated SSO-gated port under the port-per-service model (`:8444`, plus `/grafana/` embed); the dashboard container itself publishes no host port. Its state-changing and ops-forwarding routes need the edge SSO identity (trusted only from the `caddy` peer) or the `OPS_CONTROLLER_TOKEN` bearer, so other `ordo-net` containers cannot borrow its ops-controller token | Edge SSO for operators; internal callers reuse the ops-controller bearer, no dashboard-specific token |
| WEBUI_AUTH=False | Open WebUI accessible without auth | Explicit in compose env | Change default to `WEBUI_AUTH=${WEBUI_AUTH:-True}`; opt-out, not opt-in |
| Model gateway | Unauthorized model or tool access | Master key enforced at startup (`sk-` followed by 32+ characters, any class; `local` can never run again); per-consumer virtual keys with model and MCP grants | Keys in `out/secrets.env`; missing or invalid key → 401 on `/v1/*` and `/mcp` |

## AuthN / AuthZ Tiers

- **Tier 0:** No auth (health endpoints, read-only model list)
- **Tier 1:** Bearer token (`OPS_CONTROLLER_TOKEN`), required on every ops-controller call except `/health`, `/healthz`, `/metrics` (Prometheus) and `/lease` (the model gateway's fallback gate), verified in constant time; a second, scoped token (`OPS_CONTROLLER_TOKEN_HERMES`) proves the `hermes` principal, limited to a route allowlist; ops-controller has no host port
- **Tier 2:** Edge SSO (Caddy oauth2-proxy + Google SSO + email allowlist) — the sign-in for every UI, including the dashboard, except Hermes (`:8447`), which runs its own Google OIDC sign-in because its desktop app cannot pass oauth2-proxy; who may sign in there is set by the Google OAuth client's audience ([runbooks/auth.md](../runbooks/auth.md)). Caddy is the *only* service publishing host ports: under the port-per-service model, Caddy publishes seven ports on `${CADDY_TAILNET_HOSTNAME}`, and one shared Google sign-in (domain-scoped cookie, one OAuth callback) covers six of them (all but Hermes's `:8447`) — `:443` front door (landing page, `/oauth2` callback, `/llm/*` and `/mcp` Bearer-token APIs, `/couchdb/*` for Obsidian LiveSync behind CouchDB's own auth, and 302s from every legacy subpath) plus one dedicated port per UI: `:8443` Open WebUI, `:8444` Dashboard (+ `/grafana/` embed), `:8446` ComfyUI, `:8447` Hermes, `:8449` LiteLLM admin UI, `:8450` Langfuse. UI service containers themselves have no host port and are reached only through their Caddy port or the internal `ordo-net`. The dashboard has no token of its own: its protected routes accept the SSO identity Caddy forwards (from the `caddy` peer only) or the `OPS_CONTROLLER_TOKEN` bearer
- **Future Tier 3:** Per-role OIDC / RBAC beyond the edge's binary allow/deny gate (if deeper multi-user separation is needed)
- **RBAC:** Currently binary (authed = full access). Future: read-only role (view logs, health) vs admin role (start/stop).

## Correlation ID Flow

1. A client sends `X-Request-ID` to the dashboard
2. The dashboard forwards it on its ops-controller calls (`services/dashboard/dashboard/app.py`, `routes_orchestration.py`)
3. Gap: ops-controller does not record it in the audit entry yet

## Secret Handling

### End-to-End

- `out/secrets.env`: gitignored, host-only, materialized by `ordo secrets materialize` from the one secret store (a SOPS file in a private repo, `site: SECRETS_SOURCE`); not committed
- Scoped delivery: no service loads `out/secrets.env` as an `env_file`. Each service's manifest lists the secret names it reads. Two channels deliver them: file secrets (`secret_files:`), where the value is materialized to `out/secrets/<key>`, bind-mounted read-only at `/run/secrets/<key>` and only the path travels as `<KEY>_FILE`; and env secrets (`secrets:`), where the renderer emits `KEY: ${KEY}` and compose interpolates it from `--env-file secrets.env` (which `ordo up` / `ordo recreate` and ops-controller always pass). Primary consumers (model-gateway, dashboard, ops-controller, the `mcp-*` servers) use files. A service holds only the secrets it reads; `tests/substrate/test_secret_scoping.py` pins the contract.
- MCP tool secrets (e.g. `OPS_CONTROLLER_TOKEN` for `orchestration` and `comfyui-mcp`): file secrets, `OPS_CONTROLLER_TOKEN_FILE`
- Agent runtime state lives in the `hermes-home` named volume (never in the repo); the Discord bot token is supplied as a file secret (`/run/secrets/discord_bot_token`, materialized from the secret store into `out/secrets/discord_bot_token`); per-user allowlists are runtime state inside that volume.
- Gateway tokens: in the secret store, delivered per service by one of the two channels above
- **Secret rotation:** `ordo secrets rotate KEY...` (or `set KEY --from-stdin` for an issued token) writes the store and materializes `out/secrets.env`, then prints the `ordo recreate --reading KEY...` that recreates every reader (a `restart` keeps the old environment). See `docs/runbooks/secrets.md`.

### Stack Secrets

| Secret | Location | Injected by | Notes |
|--------|----------|-------------|-------|
| `LITELLM_MASTER_KEY` | `out/secrets.env` | File secret (`<KEY>_FILE` → `/run/secrets/<key>`): model-gateway, model-gateway-keys. Env `KEY: ${KEY}`: comfyui | `sk-` + 32+ chars, enforced by the entrypoint; LiteLLM admin UI login |
| `LITELLM_SALT_KEY` | `out/secrets.env` | File secret (`<KEY>_FILE` → `/run/secrets/<key>`): model-gateway | Encrypts provider credentials in `litellm-db`. **Never rotate** (`ordo secrets rotate` refuses it) |
| `LITELLM_DB_PASSWORD` | `out/secrets.env` | File secret: model-gateway (`DATABASE_PASSWORD_FILE`), litellm-db (`POSTGRES_PASSWORD_FILE`) | Postgres password for `litellm-db` |
| `LITELLM_KEY_HERMES` / `_OPEN_WEBUI` / `_EDGE` | `out/secrets.env` | File secrets to model-gateway-keys; env `KEY: ${KEY}` to the consumer (agent, open-webui) | Per-consumer virtual keys, provisioned by `model-gateway-keys` |
| `OPS_CONTROLLER_TOKEN` | `out/secrets.env` | File secret (`<KEY>_FILE` → `/run/secrets/<key>`): ops-controller, dashboard, comfyui-gate, evals, `mcp-comfyui`, `mcp-orchestration`. Env: comfyui | Admin bearer for ops-controller |
| `OPS_CONTROLLER_TOKEN_HERMES` | `out/secrets.env` | File secret: ops-controller. Env: agent (as its `OPS_CONTROLLER_TOKEN`) | Scoped `hermes` principal's bearer |
| `DISCORD_BOT_TOKEN` | the secret store (`site: SECRETS_SOURCE`), materialized to `out/secrets/discord_bot_token` | Read-only bind mount → agent (`/run/secrets/discord_bot_token`) | Optional, only when Discord channel is used |
| `HF_TOKEN` | `out/secrets.env` | comfyui, ltx-trainer: `KEY: ${KEY}` from `secrets.env`. ops-controller: file secret, `HF_TOKEN_FILE=/run/secrets/hf_token` (read-only mount, path only in the environment) | Optional, for gated HF model pulls; ops-controller sends it to huggingface.co only, never to the CDN or mirror hop |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | `out/secrets.env` | Env, as `GITHUB_TOKEN`: comfyui | Optional, for ComfyUI-Manager custom-node fetches |

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

SSRF scripts live at `scripts/ssrf-egress-block.sh` (Linux engine only) and `scripts/ssrf-egress-block.ps1` (prints advice, blocks nothing).

### Where the block applies today

| Engine | Enforced? | Why |
|--------|-----------|-----|
| Docker Engine on Linux | Only if the operator runs `ssrf-egress-block.sh` as root | No bring-up step (`ordo up`, `ordo apply`, `ordo preflight`) applies or checks it, and the rules do not survive a reboot without `iptables-persistent` |
| Docker Desktop on Windows (WSL2 backend) | No | The engine runs in Docker Desktop's own `docker-desktop` distro. Its `DOCKER-USER` chain is not reachable from Windows, and rules set in a separate user WSL distro land in that distro's network namespace, not the engine's. The `docker-desktop` distro ships no `iptables` binary |
| Docker Desktop on macOS | No | The engine runs in Docker Desktop's VM |

So on the Windows host the stack runs on, every `network: stack` MCP server has unfiltered egress: the rest of `ordo-net` (including the read-only dashboard views), the LAN, the tailnet and the internet. `tests/substrate/test_mcp.py` pins that set and fails when a new server joins it without `SECURITY.md` naming it.

### Follow-up: a platform-independent fix

Docker networks, unlike iptables, behave the same on every engine. The plan, in order:

1. **Per-server internal networks for the servers that need no internet.** `qdrant-rag` (reaches `qdrant`, `inference-engine-embed`), `orchestration` (`dashboard`) and `comfyui-mcp` (`comfyui-gate`, `ops-controller`; model downloads go through ops-controller's allowlisted `/models/download`) only talk to named stack services. Replace `network: stack` with a manifest list of the services each one reaches; the renderer puts the server and exactly those services on a `<project>-mcp-<id>-net` with `internal: true`, the way `gated_upstream_net` isolates ComfyUI. Adds a network to each target, so `ordo apply` recreates them (ops-controller and dashboard included).
2. **An egress proxy for `searxng`.** `web_url_read` must reach the internet, so an internal network alone breaks it. Put `mcp-searxng` on an internal network with `searxng` and a pinned forward proxy that denies private, CGNAT, link-local and metadata destinations after DNS resolution (so rebinding cannot bypass it), and point the server at it with `HTTP_PROXY`/`HTTPS_PROXY` (`NO_PROXY=searxng`). The `searxng` web service itself also egresses and is a follow-on candidate for the same proxy.
3. **Retire or scope the iptables script** once 1 and 2 land: it targets the whole `ordo-net`, so on Linux it also cuts the agent's LAN and tailnet access (warning above).

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

Rendered today (`out/docker-compose.yml`, regenerated by `ordo render`, never hand-edited):

- `security_opt: ["no-new-privileges:true"]` and limits of 1 CPU / 2 GB on the five `mcp-*` servers; plugins that opt in (evals, html-video, langfuse) declare their own.
- Healthchecks and `restart: unless-stopped` on every long-running service (no healthcheck yet on `ops-controller` and `loki`).
- Init one-shots (`model-gateway-keys`, `langfuse-minio-lifecycle`) use `restart: on-failure`; the profiled `evals` runner uses `restart: "no"`.

Target, not rendered yet: `cap_drop: [ALL]` (adding back only what a service needs), `no-new-privileges`, read-only root filesystems and resource limits on the core services (model-gateway, model-gateway-keys, dashboard, ops-controller, agent, rag-ingestion).

## Security + Reliability Intersection

Items that are both security and reliability problems:
- Open WebUI auth default
- Per-tool `allowed_tools` narrowing not yet used (per-consumer server scoping is enforced)

**Improvements:** auth on by default for remotely reachable UIs; env-based secret resolution where possible; per-tool `allowed_tools` narrowing (per-consumer server scoping already ships via LiteLLM virtual keys); tool registration workflow; immutable audit trail for config changes.
