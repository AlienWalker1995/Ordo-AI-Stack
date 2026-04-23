# Architecture & Product Principles

## Product Principles

1. **Local-first:** Single `./compose up -d`. No cloud dependency for core flows. All data on host.
2. **Compose as source of truth:** All services in compose. Controller talks to Docker for ops; no K8s.
3. **Least privilege:** Dashboard never mounts docker.sock. Controller has minimal allowlisted actions. Non-root containers everywhere feasible. `cap_drop: [ALL]` as default; add back only what's required.
4. **One model endpoint:** OpenAI-compatible API (`/v1/chat/completions`, `/v1/embeddings`) as canonical surface. Adapters translate for Ollama, vLLM. Services should prefer gateway over direct Ollama.
5. **Pluggable providers:** Adapter interface for Ollama, vLLM, and future OpenAI-compatible endpoints. `DEFAULT_PROVIDER` env routes nameless models.
6. **Shared tools, guarded:** Central MCP registry (`registry.json`) with metadata. Per-client allowlists. Health checks; auto-disable failing tools. Secrets outside plaintext.
7. **Safe-by-default ops:** Controller token required (no default). Destructive actions require `confirm: true`. Dry-run mode. Audit log for every privileged action.
8. **Auditable by design:** Every privileged call → audit event with `ts`, `action`, `resource`, `actor`, `result`, `correlation_id`. Append-only. Exportable.
9. **Deny-by-default:** Unknown services blocked at MCP (`allow_clients: ["*"]` is explicit opt-in, not omission-default). Auth enabled where supported.
10. **Minimize breaking changes:** Existing `OLLAMA_BASE_URL` continues working; gateway is the preferred path. `servers.txt` still works; registry adds metadata on top.
11. **Observable:** Structured JSON logs from all custom services. Request IDs (`X-Request-ID`) propagated across model→ops→tool calls. Audit log as primary observability artifact for privileged actions.
12. **Explicit trade-offs:** Model gateway adds ~2–5ms proxy latency for interoperability. Controller-via-docker.sock is a high-value target but isolated behind auth and no host port. We accept the complexity for safe ops.
13. **Reliability is a first-class contract:** Agent and tool clients depend on machine-readable readiness, consistent timeouts/retries, and traceable failures across model gateway, MCP gateway, and optional bridges—without making the dashboard or ops-controller part of the normal request path.

---

## Current Architecture

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  Host  (network: ordo-ai-stack-frontend = host-accessible)                    │
│                                                                                │
│  ┌─────────────┐  ┌──────────┐  ┌──────────────────────────────────────────┐  │
│  │ Open WebUI  │  │   N8N    │  │  Hermes  gateway + dashboard             │  │
│  │ :3000       │  │ :5678    │  │  model → gateway                         │  │
│  │ → gateway   │  │ → gw     │  │  MCP tools → mcp-gateway                 │  │
│  └──────┬──────┘  └────┬─────┘  └────────────────┬─────────────────────────┘  │
│         │              │                           │                            │
│  ┌──────▼──────────────▼───────────────────────────▼──────────────────────┐   │
│  │  Model Gateway :11435  (frontend + backend)                             │   │
│  │  GET  /v1/models           — Ollama + vLLM, TTL-cached 60s             │   │
│  │  POST /v1/chat/completions — streaming, tools, X-Request-ID            │   │
│  │  POST /v1/responses        — OpenAI Responses API compat               │   │
│  │  POST /v1/completions      — legacy completions compat                 │   │
│  │  POST /v1/embeddings       — Ollama embed + vLLM pass-through          │   │
│  │  DELETE /v1/cache          — invalidate model list cache               │   │
│  └──────────────────────────────────────────────────────────────────────┘    │
│                                                                                │
│  ┌──────────────────────────────────────────────────────────────────────────┐  │
│  │  network: ordo-ai-stack-backend (internal — no direct host access)      │  │
│  │                                                                          │  │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────┐             │  │
│  │  │ Ollama :11434   │  │ Ops Controller  │  │ Qdrant :6333 │             │  │
│  │  │ (backend-only)  │  │ :9000 (int)     │  │ vector DB    │             │  │
│  │  │ expose via      │  │ docker.sock     │  │ RAG backend  │             │  │
│  │  │ overrides/      │  │ bearer auth     │  └──────────────┘             │  │
│  │  │ ollama-expose   │  │ audit log       │                               │  │
│  │  └─────────────────┘  └─────────────────┘                               │  │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────┐             │  │
│  │  │ MCP Gateway     │  │ Dashboard :8080  │  │ RAG Ingest   │             │  │
│  │  │ :8811           │  │ no docker.sock   │  │ --profile rag│             │  │
│  │  │ docker.sock     │  │ bearer auth (opt)│  │ watches      │             │  │
│  │  │ servers.txt     │  │ → ops ctrl API   │  │ data/rag-    │             │  │
│  │  │ registry.json   │  │ registry.json    │  │ input/       │             │  │
│  │  └─────────────────┘  └─────────────────┘  └──────────────┘             │  │
│  │  ┌─────────────────┐  ┌─────────────────┐                               │  │
│  │  │ vLLM (opt)      │  │ ComfyUI :8188   │                               │  │
│  │  │ overrides/      │  │ (frontend net)  │                               │  │
│  │  │ vllm.yml        │  └─────────────────┘                               │  │
│  │  └─────────────────┘                                                     │  │
│  └──────────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────────┘
```

## Components

- **Model Gateway** `:11435` — OpenAI-compatible proxy; Ollama + vLLM adapters; streaming, Responses API, completions compat, embeddings; TTL model cache; cache-bust endpoint; `X-Request-ID` propagation; throughput recording.
- **MCP Gateway** `:8811` — Docker MCP Gateway with 10s hot-reload; `registry.json` metadata reader; per-server health; docker.sock for spawning server containers.
- **Ops Controller** `:9000` (internal) — Authenticated REST; start/stop/restart/logs/pull; append-only JSONL audit log; docker.sock access with allowlisted operations only.
- **Dashboard** `:8080` — No docker.sock; calls controller for ops; model inventory + default-model management; MCP tool management + health badges; throughput stats + benchmark; hardware stats; RAG status. Auth: optional Bearer token (`DASHBOARD_AUTH_TOKEN`).
- **Ollama** `:11434` — LLM inference; backend-only by default (use `overrides/ollama-expose.yml` for Cursor/CLI access); GPU via `overrides/compute.yml`.
- **Qdrant** `:6333` — Vector database; backend-only; used by Open WebUI for RAG and by `rag-ingestion` service.
- **RAG Ingestion** — Watch-mode document ingester (`--profile rag`); reads `data/rag-input/`; chunks and embeds via model gateway; stores in Qdrant.
- **Hermes** (`hermes-gateway` + `hermes-dashboard`) — Agent runtime; routes model calls through model-gateway and tool calls through mcp-gateway. State under `data/hermes/`. See [docs/hermes-agent.md](../hermes-agent.md) for setup.
- **Supporting services** — Open WebUI (`:3000`, connected to Qdrant), N8N (`:5678`), ComfyUI (`:8188`).

## Data Flows

```
Model request:    Client → Model Gateway (X-Request-ID) → [Ollama | vLLM]
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
| **G1: Any service → any model** | Done | Gateway `:11435`; Ollama + vLLM adapters; streaming, embeddings, tool-calling, Responses API. Open WebUI uses `OPENAI_API_BASE_URL` → gateway. Hermes and other clients route via the same `/v1` surface. |
| **G2: Shared tools with health** | Done | MCP Gateway + `registry.json` metadata; `GET /api/mcp/health` per-server; dashboard health badges. |
| **G3: Dashboard as control center** | Done | Ops Controller: start/stop/restart/logs/pull; no host port; bearer auth. Hardware stats, throughput benchmark, default-model management, RAG status. |
| **G4: Security + auditing** | Done | Audit JSONL. Optional Bearer auth for dashboard API. `SECURITY.md` + threat table. SSRF scripts. |
| **G5: Docker best practices** | Done | `cap_drop: [ALL]`, `security_opt`, `read_only`, `tmpfs`, log rotation, resource limits, healthchecks, explicit named networks on all custom services. |
| **G6: RAG pipeline** | Done | Qdrant vector DB. `rag-ingestion` service. Open WebUI connected to Qdrant. `GET /api/rag/status` in dashboard. |

## Remaining Gaps

| Gap | Goal | Description | Severity |
|-----|------|-------------|----------|
| `WEBUI_AUTH` defaults to `False` | G4 | Open WebUI ships open; target default is `True` | Medium |
| MCP per-client policy unenforced | G2 | `allow_clients` in registry.json not enforced at gateway level — requires Docker MCP Gateway `X-Client-ID` support | Medium |
| No CI pipeline | G5 | Smoke tests exist but no GitHub Actions workflow to run them | Low |
| mcp-gateway on frontend network | G5 | Should be backend-only for internal services; currently has host port | Low |
| Reliability / readiness contracts | G1–G2 | Health today is partly architectural; see [Reliability & Contracts](reliability-and-contracts.md) | High |

## Network Assignment

| Service | Frontend | Backend | Notes |
|---------|----------|---------|-------|
| open-webui | Y | Y | Needs model-gateway, qdrant |
| dashboard | Y | Y | Needs ollama, ops-controller, mcp-gateway |
| n8n | Y | — | |
| hermes-gateway | Y | Y | Needs model-gateway, mcp-gateway |
| hermes-dashboard | Y | — | Host port via `HERMES_DASHBOARD_PORT` |
| model-gateway | Y | Y | Frontend for external clients; backend for Ollama |
| mcp-gateway | Y | — | Has host port `:8811`; M6: move to backend-only |
| ops-controller | — | Y | Internal only; no host port |
| ollama | — | Y | Backend-only by default; `overrides/ollama-expose.yml` for Cursor |
| qdrant | — | Y | Backend-only; no host port needed for compose services |
| comfyui | Y | — | |
| rag-ingestion | — | Y | Backend-only; no ingress needed |

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
| Pinned images | `ollama:0.17.4`, `open-webui:v0.8.4`, `qdrant:v1.13.4`, etc. |
| Explicit networks | `ordo-ai-stack-frontend`, `ordo-ai-stack-backend` declared; Ollama backend-only |
| `restart: unless-stopped` | All long-running services |
| One-shot `restart: "no"` | pullers, sync services |

## Repo Structure

```
AI-toolkit/
├── dashboard/           # Ops dashboard (Python/Flask)
├── model-gateway/       # OpenAI-compat model proxy
├── ops-controller/      # Authenticated Docker lifecycle API
├── mcp/                 # MCP gateway (Dockerfile, gateway/, docs/)
├── hermes/              # Hermes agent (Dockerfile, entrypoint.sh, plugins/, seed/)
├── rag-ingestion/       # Document ingester (Dockerfile, ingest.py)
├── orchestration-mcp/   # Orchestration MCP server
├── scripts/             # detect_hardware.py, ssrf-egress-block, smoke tests
├── tests/               # Contract + smoke tests
├── product requirements docs/  # This documentation
├── docs/                # Getting started, runbooks
├── data/                # gitignored, runtime data
│   ├── mcp/             # servers.txt, registry.json
│   ├── ops-controller/  # audit.log
│   ├── qdrant/          # Vector DB storage
│   ├── rag-input/       # Drop documents here
│   └── hermes/          # Hermes runtime state
├── docker-compose.yml
├── compose              # Helper script (auto-detects hardware)
├── overrides/           # Optional compose overrides
├── .env.example
└── SECURITY.md
```

---

**See also:** [Index](index.md) for component listing.
