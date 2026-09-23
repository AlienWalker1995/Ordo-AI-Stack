# Architecture & Product Principles

## Product Principles

1. **Local-first:** Render + `docker compose -p ordo … up -d` from `out/`. No cloud dependency for core flows. All data on host.
2. **Compose as source of truth:** All services in the rendered compose (`ordo.yaml` → `ordo render` → `out/docker-compose.yml`). Controller talks to Docker for ops; no K8s.
3. **Least privilege:** Dashboard never mounts docker.sock. Controller has minimal allowlisted actions. Non-root containers everywhere feasible. `cap_drop: [ALL]` as default; add back only what's required.
4. **One model endpoint:** OpenAI-compatible API (`/v1/chat/completions`, `/v1/embeddings`) as canonical surface, fronting llama.cpp. Services should prefer the gateway over direct llama.cpp.
5. **Pluggable providers:** LiteLLM gateway fronts llama.cpp and can add future OpenAI-compatible endpoints.
6. **Shared tools, guarded:** One MCP endpoint on the model gateway, fed by `kind: mcp` plugin manifests. Per-consumer scoping via LiteLLM virtual-key MCP grants (`require_key_mcp_access_defined`). Health checks per server; secrets outside plaintext.
7. **Safe-by-default ops:** Controller token required (no default). Destructive actions require `confirm: true`. Dry-run mode. Audit log for every privileged action.
8. **Auditable by design:** Every privileged call → audit event with `ts`, `action`, `resource`, `actor`, `result`, `correlation_id`. Append-only. Exportable.
9. **Deny-by-default:** A virtual key sees only the MCP servers its grant lists (`require_key_mcp_access_defined: true`); no grant means no tools, and no key means 401. Auth enabled where supported.
10. **Minimize breaking changes:** The OpenAI-compatible gateway surface is the preferred path for model access. MCP servers are declared once in `ordo.yaml`'s `plugins:` list and rendered into both the LiteLLM fragment and the dashboard's server list.
11. **Observable:** Structured JSON logs from all custom services. Request IDs (`X-Request-ID`) propagated across model→ops→tool calls. Audit log as primary observability artifact for privileged actions.
12. **Explicit trade-offs:** Model gateway adds ~2–5ms proxy latency for interoperability. Controller-via-docker.sock is a high-value target but isolated behind auth and no host port. We accept the complexity for safe ops.
13. **Reliability is a first-class contract:** Agent and tool clients depend on machine-readable readiness, consistent timeouts/retries, and traceable failures across model gateway, its MCP endpoint, the individual MCP servers, and optional bridges—without making the dashboard or ops-controller part of the normal request path.

---

## Current Architecture

Port-per-service model (2026-07-24): Caddy is still the **only** service that
publishes host ports, but it now listens on **nine** SSO-gated ports on
`${CADDY_TAILNET_HOSTNAME}` instead of one — each prebuilt SPA gets the root
it was compiled for, retiring the subpath-rewrite bandaids (Open WebUI
root-catchall, Hermes header-injected base, n8n `strip_prefix` surgery,
codebase-memory nginx rewrites).

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  Host                                                                          │
│                                                                                │
│  Caddy — the ONLY host-published ports (`${CADDY_BIND}:<port>` for each)      │
│  :443 front door (landing page, /oauth2, /llm, /mcp, n8n webhook/OAuth        │
│       passthroughs, legacy-path 302s)                                        │
│  :8443 Open WebUI   :8444 Dashboard (+ /grafana/)   :8445 n8n                 │
│  :8446 ComfyUI      :8447 Hermes (at root)          :8448 codebase-memory     │
│  :8449 LiteLLM admin UI                             :8450 Langfuse            │
│  oauth2-proxy (Google SSO, one domain-scoped session across all ports)       │
│  behind it; reverse-proxies every route below                                │
└────────────────────────────────────────┬─────────────────────────────────────┘
                                          │
┌────────────────────────────────────────▼─────────────────────────────────────┐
│  network: ordo-net  (every service below, no host port of its own)          │
│                                                                                │
│  ┌─────────────┐  ┌──────────┐  ┌──────────────────────────────────────────┐  │
│  │ Open WebUI  │  │   N8N    │  │  Hermes  gateway + dashboard             │  │
│  │ :8080       │  │ :5678    │  │  model → gateway  (LITELLM_KEY_HERMES)   │  │
│  │ → gateway   │  │ → gw     │  │  tools → gateway /mcp (same key)         │  │
│  └──────┬──────┘  └────┬─────┘  └────────────────┬─────────────────────────┘  │
│         │              │                           │                            │
│  ┌──────▼──────────────▼───────────────────────────▼──────────────────────┐   │
│  │  Model Gateway :11435  (Caddy `:443/llm/*` + `:443/mcp` → LiteLLM key, │   │
│  │                         no SSO)                                        │   │
│  │  GET  /v1/models           — llama.cpp, TTL-cached 60s                 │   │
│  │  POST /v1/chat/completions — streaming, tools, X-Request-ID            │   │
│  │  POST /v1/responses        — OpenAI Responses API compat               │   │
│  │  POST /v1/completions      — legacy completions compat                 │   │
│  │  POST /v1/embeddings       — llama.cpp embeddings                      │   │
│  │  DELETE /v1/cache          — invalidate model list cache               │   │
│  │  /mcp                      - MCP aggregation, scoped per virtual key   │   │
│  │  GET /v1/mcp/server/health - per-server MCP health                     │   │
│  └────────┬──────────────────────────────────────────────┬───────────────┘    │
│           │                                               │                     │
│  ┌────────▼─────────────────┐              ┌──────────────▼───────────────┐    │
│  │ litellm-db :5432         │              │ model-gateway-keys (one-shot)│    │
│  │ Postgres: virtual keys,  │              │ provisions LITELLM_KEY_* from│    │
│  │ teams, spend             │              │ out/model-gateway/keys.json  │    │
│  └──────────────────────────┘              └──────────────────────────────┘    │
│                                                                                │
│  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────┐                  │
│  │ llama.cpp :8080 │  │ ops-controller  │  │ Qdrant :6333 │                  │
│  │ (no host port)  │  │ :9000 scheduler │  │ vector DB    │                  │
│  │ LLM inference   │  │ lifecycle API   │  │ RAG backend  │                  │
│  │ GPU via         │  │ audit log       │  └──────────────┘                  │
│  │ render engine   │  │ docker.sock     │                                     │
│  │                 │  │ (project-scoped)│                                     │
│  │                 │  │ model switch    │                                     │
│  │                 │  │                 │                                     │
│  │                 │  │                 │                                     │
│  └─────────────────┘  └─────────────────┘                                     │
│  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────┐                  │
│  │ Dashboard :8080 │  │ RAG Ingest      │  │ ComfyUI :8188│                  │
│  │ no docker.sock  │  │ --profile rag   │  │              │                  │
│  │ auth: edge SSO  │  │ watches         │  │              │                  │
│  │ → ops ctrl API  │  │ data/rag-input/ │  │              │                  │
│  │ Settings reads  │  │                 │  │              │                  │
│  │ out/mcp/        │  │                 │  │              │                  │
│  │ servers.json    │  │                 │  │              │                  │
│  └─────────────────┘  └─────────────────┘  └──────────────┘                  │
│                                                                                │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │  network: ordo-mcp-net  (`internal: true`; model-gateway is the only  │    │
│  │  member that also sits on ordo-net, so nothing else can call a       │    │
│  │  server directly)                                                     │    │
│  │                                                                        │    │
│  │  mcp-comfyui   mcp-orchestration   mcp-qdrant-rag   mcp-n8n           │    │
│  │  mcp-searxng                     → these also join ordo-net          │    │
│  │  mcp-codebase-memory   mcp-memory-vault  (internal only)             │    │
│  │                                                                        │    │
│  │  each: long-lived service, transport http on <port>/mcp, no env_file, │    │
│  │  no-new-privileges, 1 CPU / 2 GB, label ordo.mcp=true                 │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────────────┘
```

## Components

- **Model Gateway** `:11435` — OpenAI-compatible LiteLLM proxy in front of llama.cpp; streaming, Responses API, completions compat, embeddings; TTL model cache; cache-bust endpoint; `X-Request-ID` propagation; throughput recording. It also serves `/mcp`, aggregating every MCP server the caller's virtual key is granted, and `/v1/mcp/server/health`.
- **litellm-db** `:5432` (internal): Postgres holding LiteLLM's virtual keys, teams and spend. Models and MCP servers stay in the rendered config (`STORE_MODEL_IN_DB=False`); chat keeps working when the DB is down (`allow_requests_on_db_unavailable`).
- **model-gateway-keys** (one-shot): Bootstraps the per-consumer virtual keys and their model/MCP grants from `out/model-gateway/keys.json`; idempotent, and the agent waits on it (`service_completed_successfully`).
- **MCP servers** (`mcp-<server_id>`): One long-lived compose service per enabled `kind: mcp` plugin, `transport: http`, on the internal `ordo-mcp-net`. Images are digest-pinned or built from a repo build context; no `env_file` (only the manifest's own `env:` / `secrets:`), `no-new-privileges`, 1 CPU / 2 GB, healthchecked, labelled `ordo.mcp=true` / `ordo.mcp.server_id`. No container in the tool path mounts `docker.sock`.
- **Ops Controller** `:9000` (internal): `ordo serve` (`ordo/control.py`). GPU-lease scheduler, drift-safe model switch (`POST /model-config` rewrites `ordo.yaml` and re-renders), and the compose-lifecycle API the dashboard and agent call (start/stop/restart/recreate/logs/image pull), with an append-only JSONL audit log. No host port; it does not verify callers' Bearer token. Its docker.sock access is scoped to `<project>-*` containers by the `DockerBackend` guard. See [Ops Controller](component-ops-controller.md).
- **Dashboard** internal `:8080` (no host port of its own; published to the tailnet by Caddy on its own dedicated port, `${CADDY_TAILNET_HOSTNAME}:8444`, behind oauth2-proxy / Google SSO; Grafana rides the same port at `/grafana/`), No docker.sock; calls ops-controller for lifecycle actions and model switches. Five pages (Overview, Services, Models, Media, Performance, the last embedding Grafana), a Settings drawer (MCP servers + health badges, ComfyUI custom-node requirements) and a Ctrl/Cmd K command palette. Auth: the Caddy edge (oauth2-proxy / Google SSO) is the sole auth gate; no per-service dashboard token is set in this deployment. The dashboard app code retains an optional, dormant Bearer capability (`DASHBOARD_AUTH_TOKEN` + trusted-proxy header trust) that is unused here, edge SSO is the auth model, not a fallback to rely on.
- **llama.cpp** `:8080` — LLM inference; backend-only (no host port); GPU pinning resolved by the render engine (`hardware: auto` / `ordo detect`) into `out/`.
- **Qdrant** `:6333` — Vector database; backend-only; used by Open WebUI for RAG and by `rag-ingestion` service.
- **RAG Ingestion** — Watch-mode document ingester (`--profile rag`); reads `data/rag-input/`; chunks and embeds via model gateway; stores in Qdrant.
- **Hermes** (`agent` + `hermes-dashboard`) — Agent runtime; routes both model calls and tool calls through `model-gateway` (`/v1/*` and `/mcp`) with its own virtual key `LITELLM_KEY_HERMES`. State under `data/hermes/`. Published to the tailnet by Caddy on its own dedicated port, `${CADDY_TAILNET_HOSTNAME}:8447/` (served at its origin root behind a plain SSO reverse_proxy — no forwarded-prefix base injection). See [docs/hermes-agent.md](../hermes-agent.md) for setup.
- **Supporting services** — Open WebUI (internal `:8080`, connected to Qdrant, published at `${CADDY_TAILNET_HOSTNAME}:8443`), N8N (internal `:5678`, published at `${CADDY_TAILNET_HOSTNAME}:8445`; public webhook/OAuth-callback base stays `:443/n8n/*`), ComfyUI (internal `:8188`, published at `${CADDY_TAILNET_HOSTNAME}:8446`).

## Data Flows

```
Model request:    Client → Caddy :443/llm/* (bearer key) → Model Gateway (X-Request-ID) → llama.cpp
                                      ↓ throughput
                                  Dashboard /api/throughput/record

Tool call:        Client → Caddy :443/mcp (LiteLLM key) → Model Gateway /mcp (key MCP grant check) → mcp-<server> on ordo-mcp-net

Ops action:       Dashboard → Ops Controller → Docker socket (project-scoped)
                                      ↓ audit event
                              data/ops-controller/audit.log

Audit query:      GET /audit on Ops Controller → reads JSONL

UI request:       Browser → Caddy :<service-port> (Google SSO, domain-scoped
                   session) → service container, served at its own root
```

## Goal Satisfaction (Confirmed by Code)

| Goal | Status | Evidence |
|------|--------|----------|
| **G1: Any service → any model** | Done | Gateway `:11435` fronting llama.cpp; streaming, embeddings, tool-calling, Responses API. Open WebUI uses `OPENAI_API_BASE_URL` → gateway. Hermes and other clients route via the same `/v1` surface. |
| **G2: Shared tools with health** | Done | LiteLLM MCP gateway on `model-gateway` aggregating the `mcp-*` services; `GET /api/mcp/health` reads `/v1/mcp/server/health` + `tools/list` `server_outcomes`; dashboard health badges. |
| **G3: Dashboard as control center** | Done | Ops Controller lifecycle API: start/stop/restart/recreate/logs/pull; no host port. Model switch from the Models page. Hardware stats, throughput benchmark, RAG status. |
| **G4: Security + auditing** | Done | Audit JSONL. Dashboard auth is the Caddy edge (oauth2-proxy / Google SSO); no per-service dashboard token in this deployment (app code retains a dormant, unused optional Bearer capability). `SECURITY.md` + threat table. SSRF scripts. |
| **G5: Docker best practices** | Done | `cap_drop: [ALL]`, `security_opt`, `read_only`, `tmpfs`, log rotation, resource limits, healthchecks, explicit named networks on all custom services. |
| **G6: RAG pipeline** | Done | Qdrant vector DB. `rag-ingestion` service. Open WebUI connected to Qdrant. `GET /api/rag/status` in dashboard. |

## Remaining Gaps

| Gap | Goal | Description | Severity |
|-----|------|-------------|----------|
| `WEBUI_AUTH` defaults to `False` | G4 | Open WebUI ships open; target default is `True` | Medium |
| Per-tool restrictions unused | G2 | Per-consumer MCP scoping is enforced by LiteLLM virtual-key grants (`require_key_mcp_access_defined: true`); the manifest's `allowed_tools` (per-tool narrowing) is supported by the schema but set by no manifest yet | Low |
| MCP server network isolation | G5 | Closed: the `mcp-*` services sit on `ordo-mcp-net` (`internal: true`), joined only by `model-gateway`, and publish no host port. External clients reach `/mcp` through Caddy `:443` with a LiteLLM key | Low |
| Reliability / readiness contracts | G1–G2 | Health today is partly architectural; see [Reliability & Contracts](reliability-and-contracts.md) | High |

## Network Assignment

All services run on a single Docker network, `ordo-net`. **Caddy is the only
service the stack publishes to the host** — but since the 2026-07-24
port-per-service model, Caddy itself listens on **nine** host ports
(`${CADDY_BIND}:443` plus `${CADDY_BIND}:8443`–`:8450`), one per UI plus the
`:443` front door. Every UI and API still reaches the outside world
exclusively through Caddy; nothing else binds a host port at all. Each
prebuilt SPA is served at the root it was compiled for — `:8443` Open WebUI,
`:8444` Dashboard (+ `/grafana/` embed), `:8445` n8n, `:8446` ComfyUI, `:8447`
Hermes, `:8448` codebase-memory, `:8449` LiteLLM admin UI, `:8450` Langfuse,
all at their origin root behind a plain
`import sso_service <upstream>` — retiring the subpath-rewrite bandaids
(Open WebUI root-catchall, Hermes header-injected base, n8n `strip_prefix`,
codebase-memory nginx `sub_filter` rewrites) that the single-port model
needed. The n8n, Hermes, and codebase-memory adapters were the last to
converge: their subpath surgery is now deleted, so every one of the eight UIs
serves at root (the only surviving edge path-handling is Grafana's native
same-origin `/grafana/` embed and n8n's external `:443/n8n` webhook/OAuth
URLs — see below).

`:443` remains the front door: the landing page, the one Google OAuth
callback (`/oauth2/*`), the two bearer-gated programmatic APIs
(`/llm/*`, `/mcp`), n8n's external webhook/OAuth-callback passthroughs
(`/n8n/webhook/*`, `/n8n/rest/oauth2-credential/callback*` — unchanged so
nothing external needs re-registration), and 302 redirects from every
legacy subpath (`/chat`, `/dash`, `/comfy`, `/hermes`, `/codebase-memory`,
`/grafana`, `/n8n`) to the new port, so old bookmarks keep working.

All user-facing UIs sit behind oauth2-proxy / Google SSO, one Google
sign-in for all nine ports **and the clean per-service tailnet names**: the
oauth2-proxy session cookie is domain-scoped (port-agnostic), the OAuth
callback always stays on `:443`, and the SSO gate's `rd=` redirect carries
`{host}` (portless, not `{hostport}`), so a single wildcard
`--whitelist-domain=.<domain>` + `--cookie-domain=.<domain>` in the
oauth2-proxy plugin config covers every port and every clean sidecar name at
once (the old per-port whitelist is retired). The Google OAuth client itself
needs no new redirect URIs. Two programmatic surfaces
bypass interactive SSO (a CLI/IDE client can't do a Google login) but still
go through Caddy `:443`, gated by their own bearer token instead:
model-gateway at `/llm/*` and at `/mcp` (a LiteLLM key: `LITELLM_KEY_EDGE`
for external clients, or the master key). Nothing binds `127.0.0.1` or any
other host address directly: model-gateway, litellm-db, the `mcp-*` servers
and qdrant publish no host port at all.

**Uniform serving contract.** Every UI serves at its origin root behind a
plain SSO reverse_proxy; no edge- or sidecar-side path rewriting
(`handle_path`/`strip_prefix`, `X-Forwarded-Prefix` injection, `sub_filter`)
in a UI block — enforced by `tests/test_caddyfile_invariants.py`. Two
permanent exceptions: Grafana's native same-origin `/grafana/` embed (the
dashboard iframe expects that prefix), and n8n's external
`:443/n8n/webhook/*` + `:443/n8n/rest/oauth2-credential/callback*` URLs
(registered outside the stack, so they stay put).

| Service | Host port | Notes |
|---------|-----------|-------|
| caddy | `${CADDY_BIND}:443`, `:8443`–`:8450` | The only host-published ports in the stack (nine total: the `:443` front door plus one per UI service). Bound to `0.0.0.0` (operator-approved 2026-07-17 for LAN reachability on an internet-dark network — see `docs/runbooks/auth.md`); the `${CADDY_BIND:?...}` failsafe only rejects an empty/unset value, it does not distinguish a tailnet IP from `0.0.0.0`. Reverse-proxies everything else with forward_auth → oauth2-proxy |
| oauth2-proxy | — | Internal; sits behind Caddy; Google SSO with email allowlist (`auth/oauth2-proxy/emails.txt`); one domain-scoped session covers all nine Caddy ports |
| open-webui | — | Reached at `https://<tailnet>:8443/` (its own port, served at its compiled root); needs model-gateway, qdrant |
| dashboard | — | Reached at `https://<tailnet>:8444/` (Grafana embed at `.../grafana/` on the same port); needs llamacpp, ops-controller, model-gateway |
| n8n | — | UI reached at `https://<tailnet>:8445/`; public webhook base and OAuth-callback URL stay on `:443` (`https://<tailnet>/n8n/webhook/*`, `.../n8n/rest/oauth2-credential/callback*`, unchanged so nothing external needs re-registration) |
| agent (Hermes) | — | No UI; needs model-gateway, model-gateway-keys |
| hermes-dashboard | — | Reached at `https://<tailnet>:8447/` (served at its origin root behind a plain SSO proxy) |
| model-gateway | — | No host port; reached internally at `http://model-gateway:11435` on `ordo-net`, and externally via Caddy `:443/llm/*` (bearer key, no SSO) |
| litellm-db | — | Internal only; Postgres for LiteLLM virtual keys, teams and spend; reached at `litellm-db:5432` on `ordo-net` |
| `mcp-*` (seven MCP servers) | — | No host port; on `ordo-mcp-net` (`internal: true`), reachable only by model-gateway, which serves them at `:443/mcp` (LiteLLM key, no SSO) |
| ops-controller | — | Internal only; no host port |
| llamacpp | — | Backend-only; no host port; GPU pinning resolved by the render engine (`hardware: auto` / `ordo detect`) into `out/` |
| qdrant | — | Internal only; reached at `http://qdrant:6333` on `ordo-net` |
| searxng | — | Internal only; queried by the `searxng` MCP server at `http://searxng:8080` |
| comfyui | — | Reached at `https://<tailnet>:8446/` (its own port, served at its compiled root) |
| rag-ingestion | — | Internal only; no ingress needed |
| grafana | — | Internal only; embedded in the dashboard at `https://<tailnet>:8444/grafana/` (same-origin iframe, `handle` not `handle_path` so the prefix Grafana expects is preserved) |
| codebase-memory-ui | — | Reached at `https://<tailnet>:8448/` (served at its origin root; the container's nginx proxies straight through, no `sub_filter`) |

## Compose Hardening

| Check | Status |
|-------|--------|
| Non-root | `model-gateway`, `dashboard`, `n8n`: `user: "1000:1000"` |
| `cap_drop: [ALL]` | `model-gateway`, `dashboard`, `ops-controller` |
| `security_opt: [no-new-privileges:true]` | `model-gateway`, `dashboard`, `ops-controller` |
| `read_only: true` + `tmpfs: [/tmp]` | `model-gateway`, `dashboard` |
| Healthchecks | All long-running services |
| Resource limits | `qdrant` (512M), `rag-ingestion` (256M), plus per-service limits on model-gateway / dashboard / comfyui |
| Log rotation | All services |
| Pinned images | `llama.cpp` (by digest), `open-webui:v0.10.1`, `qdrant:v1.18.2`, etc. |
| Explicit networks | Single `ordo-net` declared; every service attaches to it; only Caddy publishes host ports |
| `restart: unless-stopped` | All long-running services |
| One-shot `restart: "no"` | pullers, sync services |

## Repo Structure

```
ordo-ai-stack/
├── services/            # Every stack service, self-contained under services/<id>/: its render
│                        #   manifest (plugin.yaml / agent.yaml / dashboard.yaml) co-located with
│                        #   its build context (Dockerfile + sources). One dir per service, e.g.:
│   ├── dashboard/       # Ops dashboard: dashboard.yaml + dashboard/ (FastAPI backend + React/Vite SPA in frontend/)
│   ├── rag/             # Document ingester (Dockerfile, ingest.py)
│   ├── orchestration/   # Orchestration MCP server
│   ├── comfyui-mcp/     # ComfyUI MCP server
│   ├── qdrant-rag/      # Qdrant RAG MCP server
│   ├── codebase-memory/ # Headless codebase-memory MCP
│   ├── codebase-memory-ui/  # Codebase-memory 3D graph UI service
│   ├── hermes/          # Hermes agent build context (Dockerfile, entrypoint.sh, plugins/, seed/) + its agent.yaml manifest
│   └── …                # edge, model-gateway, memory-vault, n8n, searxng, monitoring, voice, …
├── ordo/                # Render substrate (Python package): `ordo render`, `ordo detect`, etc.
├── catalog/             # Curated model catalog (models.yaml)
├── auth/                # Edge auth: auth/caddy (Caddyfile), auth/oauth2-proxy (SSO allowlist)
├── config/              # Misc static config (e.g. comfyui-manager-seed.ini)
├── assets/              # Shared render/runtime helpers (lease-exec.py, tailscale-serve)
├── monitoring/          # Grafana + Prometheus config (monitoring plugin)
├── scripts/             # llamacpp runtime assets, secrets flow, smoke tests, cron-run monitors
├── tests/               # Contract + smoke tests; render-substrate tests under tests/substrate/
├── docs/                # Getting started, runbooks, docs/product requirements docs/ (this documentation); docs/operator-guide.md is the authoritative operating guide
├── data/                # gitignored, runtime data
│   │                    # (no data/mcp/: MCP config renders into out/mcp/servers.json)
│   ├── ops-controller/  # audit.log
│   ├── qdrant/          # Vector DB storage
│   ├── rag-input/       # Drop documents here
│   └── hermes/          # Hermes runtime state
├── secrets/             # SOPS-encrypted secrets (*.sops, tracked); decrypted plaintext is gitignored
├── ordo.example.yaml    # Tracked template; copy to ordo.yaml and edit
├── ordo.yaml            # Operator-real source (gitignored)
├── out/                 # `ordo render` output: docker-compose.yml, .env, secrets.env (never hand-edited)
└── AGENTS.md, CONTRIBUTING.md, SECURITY.md, CHANGELOG.md
```

Note: there is no repo-root `overrides/` — it was retired (`62540bf`), briefly and accidentally
re-added by the flatten commit `2d4bd9c`, then removed again and gitignored in the audit-remediation
pass (`9251e47`).

---

**See also:** [Index](index.md) for component listing.
