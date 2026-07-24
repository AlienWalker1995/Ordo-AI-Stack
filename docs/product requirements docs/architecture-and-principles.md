# Architecture & Product Principles

## Product Principles

1. **Local-first:** Render + `docker compose -p ordo … up -d` from `out/`. No cloud dependency for core flows. All data on host.
2. **Compose as source of truth:** All services in the rendered compose (`ordo.yaml` → `ordo render` → `out/docker-compose.yml`). Controller talks to Docker for ops; no K8s.
3. **Least privilege:** Dashboard never mounts docker.sock. Controller has minimal allowlisted actions. Non-root containers everywhere feasible. `cap_drop: [ALL]` as default; add back only what's required.
4. **One model endpoint:** OpenAI-compatible API (`/v1/chat/completions`, `/v1/embeddings`) as canonical surface, fronting llama.cpp. Services should prefer the gateway over direct llama.cpp.
5. **Pluggable providers:** LiteLLM gateway fronts llama.cpp and can add future OpenAI-compatible endpoints.
6. **Shared tools, guarded:** Central MCP registry (`registry.json`) with metadata. Per-client allowlists. Health checks; auto-disable failing tools. Secrets outside plaintext.
7. **Safe-by-default ops:** Controller token required (no default). Destructive actions require `confirm: true`. Dry-run mode. Audit log for every privileged action.
8. **Auditable by design:** Every privileged call → audit event with `ts`, `action`, `resource`, `actor`, `result`, `correlation_id`. Append-only. Exportable.
9. **Deny-by-default:** Unknown services blocked at MCP (`allow_clients: ["*"]` is explicit opt-in, not omission-default). Auth enabled where supported.
10. **Minimize breaking changes:** The OpenAI-compatible gateway surface is the preferred path for model access. `servers.txt` still works; registry adds metadata on top.
11. **Observable:** Structured JSON logs from all custom services. Request IDs (`X-Request-ID`) propagated across model→ops→tool calls. Audit log as primary observability artifact for privileged actions.
12. **Explicit trade-offs:** Model gateway adds ~2–5ms proxy latency for interoperability. Controller-via-docker.sock is a high-value target but isolated behind auth and no host port. We accept the complexity for safe ops.
13. **Reliability is a first-class contract:** Agent and tool clients depend on machine-readable readiness, consistent timeouts/retries, and traceable failures across model gateway, MCP gateway, and optional bridges—without making the dashboard or ops-controller part of the normal request path.

---

## Current Architecture

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  Host                                                                          │
│                                                                                │
│  Caddy :443 — the ONLY host-published port (`${CADDY_BIND}:443`)              │
│  oauth2-proxy (Google SSO) behind it; reverse-proxies every route below       │
└────────────────────────────────────────┬─────────────────────────────────────┘
                                          │
┌────────────────────────────────────────▼─────────────────────────────────────┐
│  network: ordo-net  (single Docker network — every service below, no host   │
│  port of its own)                                                            │
│                                                                                │
│  ┌─────────────┐  ┌──────────┐  ┌──────────────────────────────────────────┐  │
│  │ Open WebUI  │  │   N8N    │  │  Hermes  gateway + dashboard             │  │
│  │ :3000       │  │ :5678    │  │  model → gateway                         │  │
│  │ → gateway   │  │ → gw     │  │  MCP tools → mcp-gateway                 │  │
│  └──────┬──────┘  └────┬─────┘  └────────────────┬─────────────────────────┘  │
│         │              │                           │                            │
│  ┌──────▼──────────────▼───────────────────────────▼──────────────────────┐   │
│  │  Model Gateway :11435  (Caddy `/llm/*` → bearer key, no SSO)            │   │
│  │  GET  /v1/models           — llama.cpp, TTL-cached 60s                 │   │
│  │  POST /v1/chat/completions — streaming, tools, X-Request-ID            │   │
│  │  POST /v1/responses        — OpenAI Responses API compat               │   │
│  │  POST /v1/completions      — legacy completions compat                 │   │
│  │  POST /v1/embeddings       — llama.cpp embeddings                      │   │
│  │  DELETE /v1/cache          — invalidate model list cache               │   │
│  └──────────────────────────────────────────────────────────────────────┘    │
│                                                                                │
│  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────┐                  │
│  │ llama.cpp :8080 │  │ ops-api :9000   │  │ Qdrant :6333 │                  │
│  │ (no host port)  │  │ docker.sock     │  │ vector DB    │                  │
│  │ LLM inference   │  │ bearer auth     │  │ RAG backend  │                  │
│  │ GPU via         │  │ audit log       │  └──────────────┘                  │
│  │ render engine   │  ├─────────────────┤                                     │
│  │                 │  │ ops-controller  │                                     │
│  │                 │  │ :9000 scheduler │                                     │
│  │                 │  │ (no auth, no    │                                     │
│  │                 │  │  docker verbs)  │                                     │
│  └─────────────────┘  └─────────────────┘                                     │
│  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────┐                  │
│  │ MCP Gateway     │  │ Dashboard :8080  │  │ RAG Ingest   │                  │
│  │ :8811           │  │ no docker.sock   │  │ --profile rag│                  │
│  │ docker.sock     │  │ auth: edge SSO   │  │ watches      │                  │
│  │ servers.txt     │  │ → ops ctrl API   │  │ data/rag-    │                  │
│  │ registry.json   │  │ registry.json    │  │ input/       │                  │
│  │ Caddy `/mcp` →  │  │                  │  │              │                  │
│  │ bearer token    │  │                  │  │              │                  │
│  └─────────────────┘  └─────────────────┘  └──────────────┘                  │
│  ┌─────────────────┐                                                          │
│  │ ComfyUI :8188   │                                                          │
│  └─────────────────┘                                                          │
└────────────────────────────────────────────────────────────────────────────────┘
```

## Components

- **Model Gateway** `:11435` — OpenAI-compatible LiteLLM proxy in front of llama.cpp; streaming, Responses API, completions compat, embeddings; TTL model cache; cache-bust endpoint; `X-Request-ID` propagation; throughput recording.
- **MCP Gateway** `:8811` — Docker MCP Gateway with 10s hot-reload; `registry.json` metadata reader; per-server health; docker.sock for spawning server containers.
- **Ops Controller** `:9000` (internal) — Authenticated REST; start/stop/restart/logs/pull; append-only JSONL audit log; docker.sock access with allowlisted operations only.
- **Dashboard** internal `:8080` (no host port published; reached via Caddy front door at `${CADDY_TAILNET_HOSTNAME}/dash/` behind oauth2-proxy / Google SSO) — No docker.sock; calls controller for ops; model inventory + default-model management; MCP tool management + health badges; throughput stats + benchmark; hardware stats; RAG status. Auth: the Caddy edge (oauth2-proxy / Google SSO) is the sole auth gate; no per-service dashboard token is set in this deployment. The dashboard app code retains an optional, dormant Bearer capability (`DASHBOARD_AUTH_TOKEN` + trusted-proxy header trust) that is unused here — edge SSO is the auth model, not a fallback to rely on.
- **llama.cpp** `:8080` — LLM inference; backend-only (no host port); GPU pinning resolved by the render engine (`hardware: auto` / `ordo detect`) into `out/`.
- **Qdrant** `:6333` — Vector database; backend-only; used by Open WebUI for RAG and by `rag-ingestion` service.
- **RAG Ingestion** — Watch-mode document ingester (`--profile rag`); reads `data/rag-input/`; chunks and embeds via model gateway; stores in Qdrant.
- **Hermes** (`agent` + `hermes-dashboard`) — Agent runtime; routes model calls through model-gateway and tool calls through mcp-gateway. State under `data/hermes/`. See [docs/hermes-agent.md](../hermes-agent.md) for setup.
- **Supporting services** — Open WebUI (`:3000`, connected to Qdrant), N8N (`:5678`), ComfyUI (`:8188`).

## Data Flows

```
Model request:    Client → Model Gateway (X-Request-ID) → llama.cpp
                                      ↓ throughput
                                  Dashboard /api/throughput/record

Tool call:        Client → MCP Gateway (registry policy check) → MCP server container

Ops action:       Dashboard → Ops Controller (Bearer auth) → Docker socket
                                      ↓ audit event
                              data/ops-controller/audit.log

Audit query:      Dashboard → GET /audit (auth) → Controller reads JSONL
```

## Goal Satisfaction (Confirmed by Code)

| Goal | Status | Evidence |
|------|--------|----------|
| **G1: Any service → any model** | Done | Gateway `:11435` fronting llama.cpp; streaming, embeddings, tool-calling, Responses API. Open WebUI uses `OPENAI_API_BASE_URL` → gateway. Hermes and other clients route via the same `/v1` surface. |
| **G2: Shared tools with health** | Done | MCP Gateway + `registry.json` metadata; `GET /api/mcp/health` per-server; dashboard health badges. |
| **G3: Dashboard as control center** | Done | Ops Controller: start/stop/restart/logs/pull; no host port; bearer auth. Hardware stats, throughput benchmark, default-model management, RAG status. |
| **G4: Security + auditing** | Done | Audit JSONL. Dashboard auth is the Caddy edge (oauth2-proxy / Google SSO); no per-service dashboard token in this deployment (app code retains a dormant, unused optional Bearer capability). `SECURITY.md` + threat table. SSRF scripts. |
| **G5: Docker best practices** | Done | `cap_drop: [ALL]`, `security_opt`, `read_only`, `tmpfs`, log rotation, resource limits, healthchecks, explicit named networks on all custom services. |
| **G6: RAG pipeline** | Done | Qdrant vector DB. `rag-ingestion` service. Open WebUI connected to Qdrant. `GET /api/rag/status` in dashboard. |

## Remaining Gaps

| Gap | Goal | Description | Severity |
|-----|------|-------------|----------|
| `WEBUI_AUTH` defaults to `False` | G4 | Open WebUI ships open; target default is `True` | Medium |
| MCP per-client policy unenforced | G2 | `allow_clients` in registry.json not enforced at gateway level — requires Docker MCP Gateway `X-Client-ID` support | Medium |
| mcp-gateway on frontend network | G5 | Should be backend-only for internal services; currently published on `127.0.0.1:8811` (localhost-only) so host MCP clients (Cline / VS Code) still work, but no LAN exposure | Low |
| Reliability / readiness contracts | G1–G2 | Health today is partly architectural; see [Reliability & Contracts](reliability-and-contracts.md) | High |

## Network Assignment

All services run on a single Docker network, `ordo-net`. Caddy `:443` (`${CADDY_BIND}:443`) is the **only** port the stack publishes to the host — every UI and API reaches the outside world exclusively through the Caddy front door. User-facing UIs (dashboard, Open WebUI, n8n, ComfyUI, hermes-dashboard) sit behind oauth2-proxy / Google SSO at `${CADDY_TAILNET_HOSTNAME}`. Two programmatic surfaces bypass interactive SSO (a CLI/IDE client can't do a Google login) but still go through Caddy, gated by their own bearer token instead: model-gateway at `/llm/*` (LiteLLM's `LITELLM_MASTER_KEY`) and mcp-gateway at `/mcp` (`MCP_GATEWAY_TOKEN`). Nothing binds `127.0.0.1` or any other host address directly — model-gateway, mcp-gateway, and qdrant publish no host port at all.

| Service | Host port | Notes |
|---------|-----------|-------|
| caddy | `${CADDY_BIND}:443` | The only host-published port in the stack. Bound to `0.0.0.0` (operator-approved 2026-07-17 for LAN reachability on an internet-dark network — see `docs/runbooks/auth.md`); the `${CADDY_BIND:?...}` failsafe only rejects an empty/unset value, it does not distinguish a tailnet IP from `0.0.0.0`. Reverse-proxies everything else with forward_auth → oauth2-proxy |
| oauth2-proxy | — | Internal; sits behind Caddy; Google SSO with email allowlist (`auth/oauth2-proxy/emails.txt`) |
| open-webui | — | Reached at `https://<tailnet>/` (root catch-all in Caddy); needs model-gateway, qdrant |
| dashboard | — | Reached at `https://<tailnet>/dash/`; needs llamacpp, ops-controller, mcp-gateway |
| n8n | — | Reached at `https://<tailnet>/n8n/`; OAuth callbacks bypass auth via `/n8n/rest/oauth2-credential/callback*` |
| agent (Hermes) | — | No UI; needs model-gateway, mcp-gateway |
| hermes-dashboard | — | Reached at `https://<tailnet>/hermes/` |
| model-gateway | — | No host port; reached internally at `http://model-gateway:11435` on `ordo-net`, and externally via Caddy `/llm/*` (bearer key, no SSO) |
| mcp-gateway | — | No host port; reached internally at `http://mcp-gateway:8811` on `ordo-net`, and externally via Caddy `/mcp` (bearer token, no SSO) |
| ops-controller | — | Internal only; no host port |
| llamacpp | — | Backend-only; no host port; GPU pinning resolved by the render engine (`hardware: auto` / `ordo detect`) into `out/` |
| qdrant | — | Internal only; reached at `http://qdrant:6333` on `ordo-net` |
| searxng | — | Internal only; queried by the `searxng` MCP server at `http://searxng:8080` |
| comfyui | — | Reached at `https://<tailnet>/comfy/` |
| rag-ingestion | — | Internal only; no ingress needed |

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
| Explicit networks | `ordo-ai-stack-frontend`, `ordo-ai-stack-backend` declared; llama.cpp backend-only |
| `restart: unless-stopped` | All long-running services |
| One-shot `restart: "no"` | pullers, sync services |

## Repo Structure

```
ordo-ai-stack/
├── dashboard/           # Ops dashboard (FastAPI) — source for the dashboard service
├── hermes/              # Hermes agent (Dockerfile, entrypoint.sh, plugins/, seed/)
├── rag-ingestion/       # Document ingester (Dockerfile, ingest.py)
├── orchestration-mcp/   # Orchestration MCP server — builds the orchestration-mcp service
├── comfyui-mcp/         # ComfyUI MCP server — builds the comfyui-mcp service
├── ordo/                # Render substrate (Python package): `ordo render`, `ordo detect`, etc.
├── plugins/, agents/, catalog/, dashboards/, docker/, assets/, monitoring/  # Stack source (flattened to repo root 2026-07-24)
├── scripts/             # ssrf-egress-block, smoke tests, doctor scripts
├── tests/               # Contract + smoke tests; render-substrate tests under tests/substrate/
├── product requirements docs/  # This documentation
├── docs/                # Getting started, runbooks; docs/operator-guide.md is the authoritative operating guide; docs/history/ holds archival cutover notes
├── data/                # gitignored, runtime data
│   ├── mcp/             # servers.txt, registry.json
│   ├── ops-controller/  # audit.log
│   ├── qdrant/          # Vector DB storage
│   ├── rag-input/       # Drop documents here
│   └── hermes/          # Hermes runtime state
├── ordo.example.yaml    # Tracked template; copy to ordo.yaml and edit
├── ordo.yaml            # Operator-real source (gitignored)
├── out/                 # `ordo render` output: docker-compose.yml, .env, secrets.env (never hand-edited)
└── SECURITY.md
```

---

**See also:** [Index](index.md) for component listing.
