---
authority: canonical
status: current
owner: operator
last_verified: 2026-11-12
verified_against: 1c97d8f3608b
---

# Architecture & Product Principles

## Product Principles

1. **Local-first:** `ordo render`, then `ordo up --all` from the repo root. No cloud dependency for core flows. All data on host.
2. **Compose as source of truth:** All services in the rendered compose (`ordo.yaml` → `ordo render` → `out/docker-compose.yml`). Controller talks to Docker for ops; no K8s.
3. **Least privilege:** Dashboard never mounts docker.sock. Controller has minimal allowlisted actions. Non-root containers everywhere feasible. `cap_drop: [ALL]` and read-only root filesystems are the target, but no service renders them yet; `no-new-privileges` renders for the `mcp-*` servers and plugins that opt in (see [Compose Hardening](#compose-hardening)).
4. **One model endpoint:** OpenAI-compatible API (`/v1/chat/completions`, `/v1/embeddings`) as canonical surface, fronting llama.cpp. Services should prefer the gateway over direct llama.cpp.
5. **Pluggable providers:** LiteLLM gateway fronts llama.cpp and can add future OpenAI-compatible endpoints.
6. **Shared tools, guarded:** One MCP endpoint on the model gateway, fed by `kind: mcp` plugin manifests. Per-consumer scoping via LiteLLM virtual-key MCP grants (`require_key_mcp_access_defined`). Health checks per server; secrets outside plaintext.
7. **Safe-by-default ops:** Controller token required (no default). Destructive actions require `confirm: true`. Dry-run mode. Audit log for every privileged action.
8. **Auditable by design:** Every state-changing call → one audit record with `ts`, `caller`, `action`, `target`, `result`, `status`, refusals included (schema: [data.md](../data.md#audit-log)). Append-only, size-bounded. Exportable via `GET /audit`.
9. **Deny-by-default:** A virtual key sees only the MCP servers its grant lists (`require_key_mcp_access_defined: true`); no grant means no tools, and no key means 401. Auth enabled where supported.
10. **Minimize breaking changes:** The OpenAI-compatible gateway surface is the preferred path for model access. MCP servers are declared once in `ordo.yaml`'s `plugins:` list and rendered into both the LiteLLM fragment and `out/mcp/servers.json` (the rendered server list the native-mode checklist points operators at).
11. **Observable:** Structured JSON logs from all custom services. Request IDs (`X-Request-ID`) propagated on the dashboard→ops-controller leg; the model and MCP tool legs do not carry them yet. Audit log as primary observability artifact for privileged actions.
12. **Explicit trade-offs:** Model gateway adds ~2–5ms proxy latency for interoperability. Controller-via-docker.sock is a high-value target but isolated behind auth and no host port. We accept the complexity for safe ops.
13. **Reliability is a first-class contract:** Agent and tool clients depend on machine-readable readiness, consistent timeouts/retries, and traceable failures across model gateway, its MCP endpoint, the individual MCP servers, and optional bridges—without making the dashboard or ops-controller part of the normal request path.

---

## Current Architecture

Port-per-service model (2026-07-24): Caddy is still the **only** service that
publishes host ports, but it now listens on **seven** SSO-gated ports on
`${CADDY_TAILNET_HOSTNAME}` instead of one — each prebuilt SPA gets the root
it was compiled for, retiring the subpath-rewrite bandaids (Open WebUI
root-catchall, Hermes header-injected base, codebase-memory nginx
rewrites).

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  Host                                                                          │
│                                                                                │
│  Caddy — the ONLY host-published ports (`${CADDY_BIND}:<port>` for each)      │
│  :443 front door (landing page, /oauth2, /llm, /mcp, legacy-path 302s)        │
│  :8443 Open WebUI   :8444 Dashboard (+ /grafana/)                             │
│  :8446 ComfyUI      :8447 Hermes (at root)                                    │
│  :8449 LiteLLM admin UI                             :8450 Langfuse            │
│  oauth2-proxy (Google SSO, one domain-scoped session across all ports)       │
│  behind it; reverse-proxies every route below                                │
└────────────────────────────────────────┬─────────────────────────────────────┘
                                          │
┌────────────────────────────────────────▼─────────────────────────────────────┐
│  network: ordo-net  (most services below, no host port of its own)          │
│                                                                                │
│  ┌─────────────┐                ┌──────────────────────────────────────────┐  │
│  │ Open WebUI  │                │  Hermes  gateway + dashboard             │  │
│  │ :8080       │                │  model → gateway  (LITELLM_KEY_HERMES)   │  │
│  │ → gateway   │                │  tools → gateway /mcp (same key)         │  │
│  └──────┬──────┘                └────────────────┬─────────────────────────┘  │
│         │                                          │                            │
│  ┌──────▼──────────────────────────────────────────▼──────────────────────┐   │
│  │  Model Gateway :11435  (Caddy `:443/llm/*` + `:443/mcp` → LiteLLM key, │   │
│  │                         no SSO)                                        │   │
│  │  GET  /v1/models           — the rendered model list                   │   │
│  │  POST /v1/chat/completions — streaming, tools, X-Request-ID            │   │
│  │  POST /v1/responses        — OpenAI Responses API compat               │   │
│  │  POST /v1/completions      — legacy completions compat                 │   │
│  │  POST /v1/embeddings       — llama.cpp embeddings                      │   │
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
│  │ 5 pages +       │  │                 │  │              │                  │
│  │ Ctrl/Cmd K      │  │                 │  │              │                  │
│  │ palette         │  │                 │  │              │                  │
│  └─────────────────┘  └─────────────────┘  └──────────────┘                  │
│                                                                                │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │  network: ordo-mcp-net  (`internal: true`; model-gateway is the only  │    │
│  │  member that also sits on ordo-net, so nothing else can call a       │    │
│  │  server directly)                                                     │    │
│  │                                                                        │    │
│  │  mcp-comfyui   mcp-orchestration   mcp-qdrant-rag                     │    │
│  │  mcp-searxng                     → these also join ordo-net          │    │
│  │  mcp-memory-vault  (internal only)                                   │    │
│  │                                                                        │    │
│  │  each: long-lived service, transport http on <port>/mcp, no env_file, │    │
│  │  no-new-privileges, 1 CPU / 2 GB, label ordo.mcp=true                 │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────────────┘
```

## Components

- **Model Gateway** `:11435` — OpenAI-compatible LiteLLM proxy in front of the GPU inference engine; streaming, Responses API, completions compat, embeddings; throughput recording. Model list and aliases: [services/model-gateway/README.md](../../services/model-gateway/README.md). It also serves `/mcp`, aggregating every MCP server the caller's virtual key is granted, and `/v1/mcp/server/health`.
- **litellm-db** `:5432` (internal): Postgres holding LiteLLM's virtual keys, teams and spend. Models and MCP servers stay in the rendered config (`STORE_MODEL_IN_DB=False`); chat keeps working when the DB is down (`allow_requests_on_db_unavailable`).
- **model-gateway-keys** (one-shot): Bootstraps the per-consumer virtual keys and their model/MCP grants from `out/model-gateway/keys.json`; idempotent, and the agent waits on it (`service_completed_successfully`).
- **MCP servers** (`mcp-<server_id>`): One long-lived compose service per enabled `kind: mcp` plugin, `transport: http`, on the internal `ordo-mcp-net`. Images are digest-pinned or built from a repo build context; no `env_file` (only the manifest's own `env:` / `secrets:`), `no-new-privileges`, 1 CPU / 2 GB, healthchecked, labelled `ordo.mcp=true` / `ordo.mcp.server_id`. No container in the tool path mounts `docker.sock`.
- **Ops Controller** `:9000` (internal): `ordo serve` (`ordo/control/api.py`). GPU-lease scheduler, drift-safe model switch (`POST /model-config` rewrites `ordo.yaml` and re-renders), and the compose-lifecycle API the dashboard and agent call (start/stop/restart/recreate/logs), with an append-only JSONL audit log. No host port; every call except `/health`, `/healthz`, `/metrics` and `/lease` must carry a valid Bearer token (the admin `OPS_CONTROLLER_TOKEN`, or the scoped `hermes` principal's token). Its docker.sock access is scoped to `<project>-*` containers by the `DockerBackend` guard. See [Ops Controller](component-ops-controller.md).
- **Dashboard** internal `:8080` (no host port of its own; published to the tailnet by Caddy on its own dedicated port, `${CADDY_TAILNET_HOSTNAME}:8444`, behind oauth2-proxy / Google SSO; Grafana rides the same port at `/grafana/`), No docker.sock; calls ops-controller for lifecycle actions and model switches. Five pages (Overview, Services, Models, Performance embedding Grafana, Logs) and a Ctrl/Cmd K command palette. Auth: state-changing routes and everything under `/api/ops/` and `/api/orchestration/` need the edge SSO identity (trusted only from the `caddy` peer) or the `OPS_CONTROLLER_TOKEN` bearer; health and read-only views stay open.
- **llama.cpp** `:8080` — LLM inference; backend-only (no host port); GPU pinning resolved by the render engine (`hardware: auto` / `ordo detect`) into `out/`.
- **Qdrant** `:6333` — Vector database; backend-only; used by Open WebUI for RAG and by `rag-ingestion` service.
- **RAG Ingestion** — Watch-mode document ingester (`--profile rag`); reads `data/rag-input/` (plus the memory vault and this repo's `docs/`, read-only); chunks and embeds directly against `inference-engine-embed`, not the model gateway; stores in Qdrant.
- **Hermes** (`agent`, which also runs the Hermes dashboard as an s6 service) — Agent runtime; routes both model calls and tool calls through `model-gateway` (`/v1/*` and `/mcp`) with its own virtual key `LITELLM_KEY_HERMES`. State on the `hermes-home` named volume (mounted at `/opt/data`). Published to the tailnet by Caddy on its own dedicated port, `${CADDY_TAILNET_HOSTNAME}:8447/` (served at its origin root, with Hermes's own Google sign-in so its desktop apps can connect). See [docs/hermes-agent.md](../hermes-agent.md) for setup.
- **Supporting services** — Open WebUI (internal `:8080`, connected to Qdrant, published at `${CADDY_TAILNET_HOSTNAME}:8443`), ComfyUI (internal `:8188` on `ordo-comfyui-net`, published at `${CADDY_TAILNET_HOSTNAME}:8446` through the `comfyui-gate` GPU-admission proxy).

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
| **G2: Shared tools with health** | Done | LiteLLM MCP gateway on `model-gateway` aggregating the `mcp-*` services; per-server health from `GET /v1/mcp/server/health` + `tools/list` `server_outcomes`, visible in the LiteLLM UI. |
| **G3: Dashboard as control center** | Done | Ops Controller lifecycle API: start/stop/restart/recreate/logs; no host port. Model switch from the Models page. Hardware stats, throughput benchmark, Qdrant document count on Overview. |
| **G4: Security + auditing** | Done | Audit JSONL. Dashboard auth is the Caddy edge (oauth2-proxy / Google SSO); the dashboard also accepts the `OPS_CONTROLLER_TOKEN` bearer for non-browser callers. `SECURITY.md` + threat table. SSRF scripts. |
| **G5: Docker best practices** | Partial | Log rotation, `restart` policies and explicit named networks on every service; healthchecks on every long-running service except `ops-controller` and `loki`; resource limits and `no-new-privileges` only on the `mcp-*` servers and a few plugins; no `cap_drop` or `read_only` anywhere (see [Compose Hardening](#compose-hardening)). |
| **G6: RAG pipeline** | Done | Qdrant vector DB. `rag-ingestion` service. Open WebUI connected to Qdrant. Document count on the dashboard Overview. |

## Remaining Gaps

| Gap | Goal | Description | Severity |
|-----|------|-------------|----------|
| `WEBUI_AUTH` defaults to `False` | G4 | Open WebUI ships open; target default is `True` | Medium |
| Per-tool restrictions unused | G2 | Per-consumer MCP scoping is enforced by LiteLLM virtual-key grants (`require_key_mcp_access_defined: true`); the manifest's `allowed_tools` (per-tool narrowing) is supported by the schema but set by no manifest yet | Low |
| Container hardening not rendered | G5 | No service renders `cap_drop: [ALL]`, `read_only` or `tmpfs`; core services (model-gateway, dashboard, ops-controller, agent, rag-ingestion) carry no `no-new-privileges` or resource limits | Medium |
| The agent holds a Docker socket | G5 | The `agent` service mounts `/var/run/docker.sock`, and its read-write code mirror can reach the render output. Removal is decided and sequenced in [ADR-0001: The agent holds no Docker socket](adr-0001-agent-holds-no-docker-socket.md) | High |
| MCP server network isolation | G5 | Closed: the `mcp-*` services sit on `ordo-mcp-net` (`internal: true`), joined only by `model-gateway`, and publish no host port. External clients reach `/mcp` through Caddy `:443` with a LiteLLM key | Low |

## Network Assignment

Services share the `ordo-net` Docker network, with four narrower networks beside it: `ordo-mcp-net` (internal; model-gateway and the `mcp-*` servers), `ordo-comfyui-net` (comfyui behind `comfyui-gate`), `ordo-monitoring-logs` (internal; loki, alloy, grafana) and `ordo-monitoring-docker-api` (internal; the socket proxy and alloy). **Caddy is the only
service the stack publishes to the host** — but since the 2026-07-24
port-per-service model, Caddy itself listens on **seven** host ports
(`${CADDY_BIND}:443` plus `${CADDY_BIND}:8443`–`:8450`, a range that skips
`:8445` and `:8448`), one per UI plus the `:443` front door. Every UI and API still reaches the outside world
exclusively through Caddy; nothing else binds a host port at all. Each
prebuilt SPA is served at the root it was compiled for — `:8443` Open WebUI,
`:8444` Dashboard (+ `/grafana/` embed), `:8446` ComfyUI, `:8447`
Hermes, `:8449` LiteLLM admin UI, `:8450` Langfuse,
all at their origin root behind a plain
`import sso_service <upstream>` — retiring the subpath-rewrite bandaids
(Open WebUI root-catchall, Hermes header-injected base, codebase-memory
nginx `sub_filter` rewrites) that the single-port model needed. The Hermes
and codebase-memory adapters were the last to converge: their subpath surgery
is now deleted, so every one of the six UIs serves at root (the only
surviving edge path-handling is Grafana's native same-origin `/grafana/`
embed — see below).

`:443` remains the front door: the landing page, the one Google OAuth
callback (`/oauth2/*`), the two bearer-gated programmatic APIs
(`/llm/*`, `/mcp`), the CouchDB route for Obsidian LiveSync (`/couchdb/*`, CouchDB's own auth,
Fauxton blocked), `/healthz`, and 302 redirects from every
legacy subpath (`/chat`, `/dash`, `/comfy`, `/hermes`,
`/grafana`) to the new port, so old bookmarks keep working.

Every user-facing UI except Hermes (`:8447`, its own OIDC sign-in) sits behind oauth2-proxy / Google SSO, one Google
sign-in for the other six ports **and the clean per-service tailnet names**: the
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
in a UI block — enforced by `tests/test_caddyfile_invariants.py`. One
permanent exception: Grafana's native same-origin `/grafana/` embed (the
dashboard iframe expects that prefix).

| Service | Host port | Notes |
|---------|-----------|-------|
| caddy | `${CADDY_BIND}:443`, `:8443`–`:8450` | The only host-published ports in the stack (seven total: the `:443` front door plus one per UI service; the `:8443`–`:8450` range skips `:8445` and `:8448`). Bound to the operator's `CADDY_BIND`: a tailnet address, or `0.0.0.0` for LAN reachability on a network with no forwarded ports (see `docs/runbooks/auth.md`); the `${CADDY_BIND:?...}` failsafe only rejects an empty/unset value, it does not distinguish a tailnet IP from `0.0.0.0`. Reverse-proxies everything else with forward_auth → oauth2-proxy |
| oauth2-proxy | — | Internal; sits behind Caddy; Google SSO with email allowlist (`site: SSO_ALLOWED_EMAILS`); one domain-scoped session covers every Caddy port except Hermes's `:8447` |
| open-webui | — | Reached at `https://<tailnet>:8443/` (its own port, served at its compiled root); needs inference-engine-gpu, model-gateway, qdrant |
| dashboard | — | Reached at `https://<tailnet>:8444/` (Grafana embed at `.../grafana/` on the same port); needs ops-controller |
| agent (Hermes) | — | Needs model-gateway, model-gateway-keys, dashboard, ops-controller |
| Hermes dashboard (in `agent`) | — | Reached at `https://<tailnet>:8447/` (served at its origin root, Hermes's own Google sign-in) |
| model-gateway | — | No host port; reached internally at `http://model-gateway:11435` on `ordo-net`, and externally via Caddy `:443/llm/*` (bearer key, no SSO) |
| litellm-db | — | Internal only; Postgres for LiteLLM virtual keys, teams and spend; reached at `litellm-db:5432` on `ordo-net` |
| `mcp-*` (five MCP servers) | — | No host port; on `ordo-mcp-net` (`internal: true`), reachable only by model-gateway, which serves them at `:443/mcp` (LiteLLM key, no SSO) |
| ops-controller | — | Internal only; no host port |
| inference-engine-gpu | — | Backend-only; no host port; GPU pinning resolved by the render engine (`hardware: auto` / `ordo detect`) into `out/` |
| qdrant | — | Internal only; reached at `http://qdrant:6333` on `ordo-net` |
| searxng | — | Internal only; queried by the `searxng` MCP server at `http://searxng:8080` |
| comfyui | — | Reached at `https://<tailnet>:8446/` through `comfyui-gate` (GPU admission), served at its compiled root |
| rag-ingestion | — | Internal only; no ingress needed |
| grafana | — | Internal only; embedded in the dashboard at `https://<tailnet>:8444/grafana/` (same-origin iframe, `handle` not `handle_path` so the prefix Grafana expects is preserved) |

## Compose Hardening

| Check | Status |
|-------|--------|
| Non-root | Not set by the render: no service declares `user:` except `agent` (`user: root`) |
| `cap_drop: [ALL]` | Not rendered for any service (target) |
| `security_opt: [no-new-privileges:true]` | The five `mcp-*` servers, plus plugins that opt in (evals, html-video, langfuse) |
| `read_only: true` + `tmpfs: [/tmp]` | Not rendered for any service (target) |
| Healthchecks | Every long-running service except `ops-controller` and `loki` |
| Resource limits | `mcp-*` (1 CPU / 2 GB), evals, html-video, langfuse-db, inference-engine-cpu (CPU count) |
| Log rotation | All services (json-file, 20m) |
| Pinned images | External images pinned by tag and digest; first-party images built from each service's build context, tags recorded in `out/images.json` |
| Explicit networks | Five named networks (see [Network Assignment](#network-assignment)); only Caddy publishes host ports |
| `restart: unless-stopped` | All long-running services |
| One-shot | `on-failure` for init one-shots (`model-gateway-keys`, `langfuse-minio-lifecycle`); `restart: "no"` only for the profiled `evals` runner |

## Repo Structure

```
ordo-ai-stack/
├── services/            # Every stack service under services/<id>/: its render manifest
│                        #   (plugin.yaml / agent.yaml / dashboard.yaml) beside its build context.
│                        #   Core services the substrate defines (inference-engine-gpu,
│                        #   model-gateway) carry no manifest, only catalog.json and sources. E.g.:
│   ├── dashboard/       # Ops dashboard: dashboard.yaml + dashboard/ (FastAPI backend + React/Vite SPA in frontend/)
│   ├── rag/             # Document ingester (Dockerfile, ingest.py)
│   ├── orchestration/   # Orchestration MCP server
│   ├── comfyui-mcp/     # ComfyUI MCP server
│   ├── qdrant-rag/      # Qdrant RAG MCP server
│   ├── hermes/          # Hermes agent build context (Dockerfile, ordo-init.sh, plugins/, seed/) + its agent.yaml manifest
│   └── …                # edge, model-gateway, memory-vault, searxng, monitoring, voice, …
├── ordo/                # Render substrate (Python package): `ordo render`, `ordo detect`, etc.
├── catalog/             # Curated model catalog (models.yaml)
├── auth/                # Edge auth: auth/caddy (Caddyfile), auth/oauth2-proxy (SSO allowlist)
├── assets/              # Shared render/runtime helpers (lease-exec.py, tailscale-serve)
├── monitoring/          # Grafana + Prometheus config (monitoring plugin)
├── scripts/             # inference-engine-gpu runtime assets, secrets flow, smoke tests, cron-run monitors
├── tests/               # Contract + smoke tests; render-substrate tests under tests/substrate/
├── docs/                # Getting started, runbooks, docs/canonical/ (this documentation); docs/operator-guide.md is the authoritative operating guide
├── data/                # gitignored, runtime data
│   │                    # (no data/mcp/: MCP config renders into out/mcp/servers.json)
│   ├── ops-controller/  # audit.log
│   ├── qdrant/          # Vector DB storage
│   └── rag-input/       # Drop documents here
├── secrets/             # README only; the operator's store is a private repo or Infisical
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
