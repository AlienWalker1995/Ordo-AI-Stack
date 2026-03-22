# AI Platform-in-a-Box — Product Requirements Document

**Status:** Living document — updated 2026-03-22
**Scope:** Local-first AI platform: unified model access, shared tools, secure ops, RAG, and agentic runtime.
**Prior revision:** 2026-03-04. All M0–M5 milestones delivered. **2026-03-22:** Added Section 10 — reliability layer, service contracts, and OpenClaw-as-mesh-client framing (strategic roadmap M7).

---

## SECTION 0 — Executive Summary

### 0.1 Product Vision

A self-hosted AI platform that any developer can run with `./compose up -d`. Core guarantees:

1. **One model endpoint** — Every service reaches every model (Ollama, vLLM, future) via a single OpenAI-compatible gateway. No per-service provider config.
2. **Shared tools with health** — MCP tools served from a central gateway with registry metadata, per-server health badges, and policy controls.
3. **Authenticated ops** — Dashboard manages the full service lifecycle through a secure, audited control plane. No docker.sock in the UI layer.
4. **RAG out of the box** — Vector search (Qdrant) is wired into Open WebUI and exposed to the gateway; document ingestion is one compose profile away.
5. **Hardened by default** — Non-root containers, `cap_drop: [ALL]`, read-only filesystems, explicit networks, log rotation, resource limits across all custom services.

### 0.2 Shipped Capabilities (as of 2026-03-04)

| Capability | Status | Key Files |
|-----------|--------|-----------|
| OpenAI-compat model gateway (Ollama + vLLM) | ✅ Live | `model-gateway/main.py` |
| Model list TTL cache + cache-bust endpoint | ✅ Live | `model-gateway/main.py` |
| `X-Request-ID` correlation end-to-end | ✅ Live | `model-gateway/main.py`, `dashboard/app.py`, `ops-controller/main.py` |
| Responses API (`/v1/responses`) | ✅ Live | `model-gateway/main.py` |
| Completions compat (`/v1/completions`) | ✅ Live | `model-gateway/main.py` |
| MCP Gateway with hot-reload | ✅ Live | `mcp/`, `docker-compose.yml` |
| MCP registry.json metadata layer | ✅ Live | `dashboard/app.py`, `data/mcp/registry.json` |
| MCP health endpoint + UI badges | ✅ Live | `dashboard/app.py` |
| Ops Controller (start/stop/restart/logs/pull) | ✅ Live | `ops-controller/main.py` |
| Append-only JSONL audit log | ✅ Live | `ops-controller/main.py` |
| Dashboard auth (Bearer) | ✅ Live | `dashboard/app.py` |
| Dashboard throughput stats + benchmark | ✅ Live | `dashboard/app.py` |
| Dashboard hardware stats | ✅ Live | `dashboard/app.py` |
| Dashboard default-model management | ✅ Live | `dashboard/app.py` |
| RAG pipeline (Qdrant + rag-ingestion) | ✅ Live | `rag-ingestion/`, `docker-compose.yml` |
| Open WebUI → Qdrant vector DB | ✅ Live | `docker-compose.yml` |
| RAG status endpoint | ✅ Live | `dashboard/app.py` |
| Docker hardening (cap_drop, read_only, networks) | ✅ Live | `docker-compose.yml` |
| Explicit frontend/backend networks | ✅ Live | `docker-compose.yml` |
| Ollama backend-only (no host port default) | ✅ Live | `docker-compose.yml`, `overrides/ollama-expose.yml` |
| SSRF egress block scripts | ✅ Live | `scripts/ssrf-egress-block.sh`, `.ps1` |
| OpenClaw agentic runtime + CLI profile | ✅ Live | `docker-compose.yml` |
| vLLM optional compose profile | ✅ Live | `overrides/vllm.yml` |
| Contract + smoke tests | ✅ Live | `tests/` |

### 0.3 Open Risks

| Risk | Severity | Status |
|------|----------|--------|
| `docker.sock` in both `mcp-gateway` and `ops-controller` | High | Accepted — mitigated by allowlist + auth + no host port |
| `WEBUI_AUTH` still defaults to `False` | Medium | Tracked — change to `True` in M6 |
| `openclaw.json` contains plaintext tokens on disk | Medium | Accepted — gitignored `data/`; documented in SECURITY.md |
| MCP per-client policy (`allow_clients`) not enforced at gateway level | Medium | Planned — requires Docker MCP Gateway `X-Client-ID` support |
| No CI pipeline for compose smoke tests | Low | Tracked — M6 |
| No first-class runtime contract between OpenClaw and dependencies | High | **Strategic — Section 10 / M7:** health semantics, circuit breakers, config validation |

### 0.4 Strategic priority: reliability layer (not “more OpenClaw features”)

The platform’s next major quality bar is a **reliability spine**: guarantees that OpenClaw (and other clients) can **reach, authenticate to, and recover from failures** across the shared stack—especially **Model Gateway `:11435`**, **MCP Gateway `:8811`**, and **browser/tool bridges** behind them.

**Design stance:** OpenClaw is **one client** on a **shared service mesh**, not the architectural center. That is the right shape; the consequence is that **service-to-service reliability and dependency management** become the dominant failure mode when they are weak (“OpenClaw feels flaky” even when the OpenClaw process is healthy).

Section 10 defines what “very strong” means for this repo, the **missing contract layer**, concrete requirements for model/MCP/browser paths, observability, config, security/reliability balance, a **phased roadmap**, and **anti-patterns** to avoid.

---

## SECTION 1 — Current State (Grounded)

*Last verified: 2026-03-04 against `model-gateway/main.py`, `ops-controller/main.py`, `dashboard/app.py`, `docker-compose.yml`, `rag-ingestion/`, `tests/`.* **2026-03-22:** Section 10 / M7 reliability requirements added (not yet implemented).

### 1.1 Architecture Diagram (Current)

```
┌────────────────────────────────────────────────────────────────────────────────┐
│  Host  (network: ai-toolkit-frontend = host-accessible)                        │
│                                                                                │
│  ┌─────────────┐  ┌──────────┐  ┌──────────────────────────────────────────┐  │
│  │ Open WebUI  │  │   N8N    │  │  OpenClaw Gateway  :6680/:6681 (def.; secure: 18789/18790) │  │
│  │ :3000       │  │ :5678    │  │  model provider → gateway                 │  │
│  │ → gateway   │  │ → gw     │  │  MCP tools via bridge plugin              │  │
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
│  │  network: ai-toolkit-backend (internal — no direct host access)          │  │
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

### 1.2 Goal Satisfaction (Confirmed by Code)

| Goal | Status | Evidence |
|------|--------|----------|
| **G1: Any service → any model** | ✅ Done | Gateway `:11435`; Ollama + vLLM adapters; streaming, embeddings, tool-calling, Responses API. Open WebUI uses `OPENAI_API_BASE_URL` → gateway. OpenClaw routes via gateway provider. |
| **G2: Shared tools with health** | ✅ Done | MCP Gateway + `registry.json` metadata; `GET /api/mcp/health` per-server; dashboard health badges. |
| **G3: Dashboard as control center** | ✅ Done | Ops Controller: start/stop/restart/logs/pull; no host port; bearer auth. Hardware stats, throughput benchmark, default-model management, RAG status. |
| **G4: Security + auditing** | ✅ Done | Audit JSONL (`ts/action/resource/actor/result/detail/correlation_id`). Optional Bearer auth for dashboard API. `SECURITY.md` + threat table. SSRF scripts. |
| **G5: Docker best practices** | ✅ Done | `cap_drop: [ALL]`, `security_opt`, `read_only`, `tmpfs`, log rotation, resource limits, healthchecks, explicit named networks on all custom services. |
| **G6: RAG pipeline** | ✅ Done | Qdrant vector DB (backend-only). `rag-ingestion` service (drop files in `data/rag-input/`). Open WebUI connected to Qdrant. `GET /api/rag/status` in dashboard. |

### 1.3 Remaining Gaps

| Gap | Goal | Description | Severity |
|-----|------|-------------|----------|
| `WEBUI_AUTH` defaults to `False` | G4 | Open WebUI ships open; target default is `True` | Medium |
| MCP per-client policy unenforced | G2 | `allow_clients` in registry.json not enforced at gateway level — requires Docker MCP Gateway `X-Client-ID` support | Medium |
| No CI pipeline | G5 | Smoke tests exist but no GitHub Actions workflow to run them | Low |
| `openclaw.json` plaintext tokens | G4 | Telegram token, skill API keys on disk in gitignored `data/` | Low |
| mcp-gateway on frontend network | G5 | Should be backend-only for internal services; currently has host port | Low |
| Reliability / readiness contracts | G1–G2, orchestration | Health today is partly architectural; OpenClaw does not consume a unified, machine-readable **dependency registry** with L1–L3 checks, retry budgets, or circuit breakers (Section 10) | High |

### 1.4 OpenClaw: Current Integration Map (Confirmed)

| Aspect | Current State | Config Location |
|--------|---------------|-----------------|
| **Model routing** | `models.providers.gateway` (`baseUrl: http://model-gateway:11435/v1`, `api: openai-completions`); default model `gateway/ollama/qwen2.5:7b` with Google fallback | `data/openclaw/openclaw.json` |
| **MCP tools** | `openclaw-mcp-bridge` plugin → `http://mcp-gateway:8811/mcp`; tools surface as `gateway__<tool>` | `data/openclaw/openclaw.json` |
| **Config sync** | `openclaw-config-sync` runs `merge_gateway_config.py` before gateway start; adds gateway provider if missing | `docker-compose.yml` |
| **Auth** | Gateway token via `OPENCLAW_GATEWAY_TOKEN` in `.env`; gateway auth mode `token` | `.env`, `openclaw.json` |
| **Service ID header** | `headers.X-Service-Name: openclaw` → dashboard shows "openclaw" in throughput | `openclaw.json` |
| **Workspace sync** | Copies `SOUL.md`, `AGENTS.md`, `TOOLS.md` from `openclaw/workspace/` to `data/openclaw/workspace/` on startup | `docker-compose.yml` |

---

## SECTION 2 — Product Principles

1. **Local-first:** Single `./compose up -d`. No cloud dependency for core flows. All data on host.
2. **Compose as source of truth:** All services in compose. Controller talks to Docker for ops; no K8s.
3. **Least privilege:** Dashboard never mounts docker.sock. Controller has minimal allowlisted actions. Non-root containers everywhere feasible. `cap_drop: [ALL]` as default; add back only what's required.
4. **One model endpoint:** OpenAI-compatible API (`/v1/chat/completions`, `/v1/embeddings`) as canonical surface. Adapters translate for Ollama, vLLM. Services should prefer gateway over direct Ollama.
5. **Pluggable providers:** Adapter interface for Ollama, vLLM, and future OpenAI-compatible endpoints. `DEFAULT_PROVIDER` env routes nameless models.
6. **Shared tools, guarded:** Central MCP registry (`registry.json`) with metadata. Per-client allowlists. Health checks; auto-disable failing tools. Secrets outside plaintext.
7. **Safe-by-default ops:** Controller token required (no default). Destructive actions require `confirm: true`. Dry-run mode. Audit log for every privileged action.
8. **Auditable by design:** Every privileged call → audit event with `ts`, `action`, `resource`, `actor`, `result`, `correlation_id`. Append-only. Exportable.
9. **Deny-by-default:** Unknown services blocked at MCP (`allow_clients: ["*"]` is explicit opt-in, not omission-default). Auth enabled where supported.
10. **Minimize breaking changes:** Existing `OLLAMA_BASE_URL` continues working. OpenClaw `ollama` provider still works; gateway is the preferred path. `servers.txt` still works; registry adds metadata on top.
11. **Observable:** Structured JSON logs from all custom services. Request IDs (`X-Request-ID`) propagated across model→ops→tool calls. Audit log as primary observability artifact for privileged actions.
12. **Explicit trade-offs:** Model gateway adds ~2–5ms proxy latency for interoperability. Controller-via-docker.sock is a high-value target but isolated behind auth and no host port. We accept the complexity for safe ops.
13. **Reliability is a first-class contract:** Clients (including OpenClaw) depend on **machine-readable** readiness, consistent timeouts/retries, and traceable failures across model gateway, MCP gateway, and optional bridges—without making the dashboard or ops-controller part of the normal request path (Section 10).

---

## SECTION 3 — Target Architecture

### Components

- **Model Gateway** `:11435` — OpenAI-compatible proxy; Ollama + vLLM adapters; streaming, Responses API, completions compat, embeddings; TTL model cache; cache-bust endpoint; `X-Request-ID` propagation; throughput recording.
- **MCP Gateway** `:8811` — Docker MCP Gateway with 10s hot-reload; `registry.json` metadata reader; per-server health; docker.sock for spawning server containers.
- **Ops Controller** `:9000` (internal) — Authenticated REST; start/stop/restart/logs/pull; append-only JSONL audit log; docker.sock access with allowlisted operations only.
- **Dashboard** `:8080` — No docker.sock; calls controller for ops; model inventory + default-model management; MCP tool management + health badges; throughput stats + benchmark; hardware stats; RAG status. Auth: optional Bearer token (`DASHBOARD_AUTH_TOKEN`).
- **Ollama** `:11434` — LLM inference; backend-only by default (use `overrides/ollama-expose.yml` for Cursor/CLI access); GPU via `overrides/compute.yml`.
- **Qdrant** `:6333` — Vector database; backend-only; used by Open WebUI for RAG and by `rag-ingestion` service.
- **RAG Ingestion** — Watch-mode document ingester (`--profile rag`); reads `data/rag-input/`; chunks and embeds via model gateway; stores in Qdrant.
- **OpenClaw Gateway** `:6680/:6681` (default host ports; `overrides/openclaw-secure.yml`: **18789**/18790) — Agentic runtime; routes models via gateway provider; MCP tools via bridge plugin.
- **OpenClaw CLI** — Interactive CLI (`--profile openclaw-cli`); gateway token only; no session credentials.
- **Supporting services** — Open WebUI (`:3000`, connected to Qdrant), N8N (`:5678`), ComfyUI (`:8188`), openclaw sync/config/plugin services.

### Data Flows

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

### Text Diagram (Target)

```
┌───────────────────────────────────────────────────────────────────────────────┐
│  Host                                                                         │
│  ┌─────────────┐ ┌──────────┐ ┌──────────────────────────────────────────┐   │
│  │ Open WebUI  │ │   N8N    │ │  OpenClaw Gateway  :6680 (def.)            │   │
│  │ :3000       │ │ :5678    │ └───────────────────────────┬──────────────┘   │
│  └──────┬──────┘ └────┬─────┘                             │                  │
│         │             │           OPENAI_API_BASE         │ gateway provider │
│  ┌──────▼─────────────▼───────────────────────────────────▼──────────────┐   │
│  │  network: ai-toolkit-frontend (public-facing services)                │   │
│  │  ┌─────────────────────────────────────────────────────────────────┐  │   │
│  │  │  Model Gateway :11435                                           │  │   │
│  │  │  /v1/models (TTL cached)  /v1/chat/completions  /v1/embeddings  │  │   │
│  │  └──────────────────────┬──────────────────────────────────────────┘  │   │
│  └─────────────────────────┼──────────────────────────────────────────────┘  │
│  ┌─────────────────────────┼──────────────────────────────────────────────┐   │
│  │  network: ai-toolkit-backend (internal)                               │   │
│  │  ┌──────────────────────▼─────┐  ┌────────────┐  ┌─────────────────┐ │   │
│  │  │ Ollama :11434 (no host port)│  │ vLLM (opt) │  │ Ops Controller  │ │   │
│  │  └────────────────────────────┘  └────────────┘  │ :9000 (int)     │ │   │
│  │  ┌─────────────────────────────┐  ┌────────────┐  │ docker.sock     │ │   │
│  │  │ MCP Gateway :8811           │  │  Dashboard │  │ auth required   │ │   │
│  │  │ registry.json + policy      │◄─┤  :8080     │◄─┤                 │ │   │
│  │  │ docker.sock (spawn servers) │  │  no sock   │  └─────────────────┘ │   │
│  │  └─────────────────────────────┘  └────────────┘                      │   │
│  └────────────────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Key Interfaces

#### A) Model Gateway API (OpenAI-compatible)

**Base URL:** `http://model-gateway:11435`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/models` | GET | Aggregated model list (Ollama + vLLM); TTL-cached 60s |
| `/v1/chat/completions` | POST | Chat; routes by model prefix (`ollama/`, `vllm/`); streaming ✓; tool-calling ✓ |
| `/v1/responses` | POST | OpenAI Responses API — converts to chat completions + tools; streams ✓ |
| `/v1/completions` | POST | Legacy completions compat — wraps chat completions |
| `/v1/embeddings` | POST | Embeddings; Ollama `/api/embed` + vLLM pass-through |
| `/v1/cache` | DELETE | Invalidate model list cache (force re-fetch from Ollama/vLLM) |
| `/health` | GET | Gateway health; checks at least one provider reachable |

**Model naming:**
- `ollama/deepseek-r1:7b` → Ollama
- `vllm/llama3` → vLLM (if `VLLM_URL` set)
- `deepseek-r1:7b` (no prefix) → `DEFAULT_PROVIDER`

**Headers:** `X-Service-Name: <caller>` (for throughput attribution); `X-Request-ID: <uuid>` (for correlation).

**Responses API notes:** Converts Responses API input items and tool definitions to chat-completions format. Tool calls in Responses API format (`function` type with `parameters`) are re-serialized back to Responses format in the response. Unsupported tool types (e.g. `computer_use_preview`) are filtered before forwarding.

**Config:**
```yaml
# docker-compose.yml (current)
model-gateway:
  environment:
    - OLLAMA_URL=http://ollama:11434
    - VLLM_URL=${VLLM_URL:-}
    - DEFAULT_PROVIDER=ollama
    - DASHBOARD_URL=http://dashboard:8080
    - MODEL_CACHE_TTL_SEC=${MODEL_CACHE_TTL_SEC:-60}
```

#### B) Tool Registry + MCP Gateway Policy API

**Registry format** (`data/mcp/registry.json`):
```json
{
  "version": 1,
  "servers": {
    "duckduckgo": {
      "image": "mcp/duckduckgo",
      "description": "Web search via DuckDuckGo",
      "scopes": ["search"],
      "allow_clients": ["*"],
      "rate_limit_rpm": 60,
      "timeout_sec": 30,
      "env_schema": {}
    },
    "github-official": {
      "image": "mcp/github-official",
      "description": "GitHub issues, PRs, repos",
      "scopes": ["github"],
      "allow_clients": ["open-webui", "openclaw"],
      "env_schema": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": {"required": true, "secret": true}
      }
    },
    "filesystem": {
      "image": "mcp/filesystem",
      "description": "File access — requires FILESYSTEM_ROOT configured",
      "scopes": ["filesystem"],
      "allow_clients": [],
      "env_schema": {
        "FILESYSTEM_ROOT": {"required": true, "secret": false}
      }
    }
  }
}
```

**Note on filesystem:** `allow_clients: []` disables by default. This replaces the current broken state of `filesystem` being in `servers.txt` without root-dir config.

**Policy API** (dashboard `/api/mcp`) — implemented:
- `GET /api/mcp/servers` — enabled list merged with registry metadata + catalog
- `POST /api/mcp/add` — add tool (updates `servers.txt`)
- `POST /api/mcp/remove` — remove tool (updates `servers.txt`)
- `GET /api/mcp/health` — per-server health status: `{server: {ok: bool, checked_at: ts}}`

#### C) Ops Controller API

**Base URL:** `http://ops-controller:9000` (internal network; no host port)

**Auth:** `Authorization: Bearer <OPS_CONTROLLER_TOKEN>`

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/health` | GET | None | Liveness |
| `/services` | GET | None | List compose services + state |
| `/services/{id}/start` | POST | Bearer | Start (confirm: true required) |
| `/services/{id}/stop` | POST | Bearer | Stop (confirm: true required) |
| `/services/{id}/restart` | POST | Bearer | Restart (confirm: true required) |
| `/services/{id}/logs` | GET | Bearer | Tail logs (tail=100 max 500) |
| `/images/pull` | POST | Bearer | Pull images for services |
| `/mcp/containers` | GET | Bearer | List MCP server containers |
| `/audit` | GET | Bearer | Audit log (limit=50) |

**Safety:** All mutating endpoints require `{"confirm": true}`. Optional `{"dry_run": true}` returns planned action without executing.

#### D) Audit Event Pipeline

**Schema:**
```json
{
  "ts": "2026-03-01T12:34:56.789Z",
  "action": "restart",
  "resource": "ollama",
  "actor": "dashboard",
  "result": "ok",
  "detail": "",
  "correlation_id": "req-abc123"
}
```

**Action types:** `start` | `stop` | `restart` | `pull` | `logs` | `mcp_add` | `mcp_remove` | `model_pull` | `model_delete`

**Storage:** `data/ops-controller/audit.log` — JSONL, append-only. Rotate at 10MB. Export: `GET /audit?limit=N&since=ISO8601`.

**Correlation:** Model gateway generates `X-Request-ID: req-<uuid>` on every call; passes to dashboard throughput records; controller accepts optional `X-Request-ID` header and includes in audit entry.

#### E) Dashboard API (extended)

**Base URL:** `http://dashboard:8080` (`:8080` host port)

**Auth:** Bearer token (`DASHBOARD_AUTH_TOKEN`) on all `/api/*` except health, auth/config, hardware, rag/status.

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/health` | GET | None | Dashboard + upstream service health |
| `/api/hardware` | GET | None | Host hardware stats (CPU, memory, GPU via nvidia-smi) |
| `/api/auth/config` | GET | None | Auth method in use |
| `/api/rag/status` | GET | None | Qdrant collection status + point count |
| `/api/ollama/models` | GET | ✓ | Installed Ollama models |
| `/api/ollama/pull` | POST | ✓ | Pull model (streaming progress) |
| `/api/ollama/delete` | POST | ✓ | Delete Ollama model |
| `/api/ollama/library` | GET | ✓ | Pullable models from Ollama registry (24h cache) |
| `/api/ollama/ps` | GET | ✓ | Models currently loaded in Ollama |
| `/api/comfyui/models` | GET | ✓ | Installed ComfyUI models |
| `/api/comfyui/pull` | POST | ✓ | Pull ComfyUI models |
| `/api/comfyui/models/{cat}/{file}` | DELETE | ✓ | Delete ComfyUI model |
| `/api/mcp/servers` | GET | ✓ | Enabled servers + registry metadata + catalog |
| `/api/mcp/add` | POST | ✓ | Enable MCP server |
| `/api/mcp/remove` | POST | ✓ | Disable MCP server |
| `/api/mcp/health` | GET | ✓ | Per-server health status |
| `/api/services` | GET | ✓ | Compose service list via ops controller |
| `/api/ops/services/{id}/start` | POST | ✓ | Start service |
| `/api/ops/services/{id}/stop` | POST | ✓ | Stop service |
| `/api/ops/services/{id}/restart` | POST | ✓ | Restart service |
| `/api/ops/services/{id}/logs` | GET | ✓ | Tail service logs |
| `/api/ops/available` | GET | ✓ | Check ops controller reachability |
| `/api/throughput/record` | POST | ✓ | Record model call (called by model-gateway) |
| `/api/throughput/stats` | GET | ✓ | Throughput statistics |
| `/api/throughput/service-usage` | GET | ✓ | Per-service model usage |
| `/api/throughput/benchmark` | POST | ✓ | Run token throughput benchmark |
| `/api/config/default-model` | GET | ✓ | Get current default model |
| `/api/config/default-model` | POST | ✓ | Set default model (restarts open-webui) |

#### F) RAG Pipeline

**Services:** `qdrant` (`:6333`, backend-only) + `rag-ingestion` (`--profile rag`)

**Ingest flow:**
1. Drop documents into `data/rag-input/`
2. `rag-ingestion` watches directory; chunks at `RAG_CHUNK_SIZE` tokens (default 400, overlap 50)
3. Embeds via model gateway (`EMBED_MODEL`, default `nomic-embed-text`)
4. Stores in Qdrant collection (`RAG_COLLECTION`, default `documents`)

**Query flow:** Open WebUI → Qdrant (`VECTOR_DB=qdrant`, `QDRANT_URI=http://qdrant:6333`) — configured automatically in compose.

**Status:** `GET /api/rag/status` → `{ok, collection, points_count, status}` — auth-exempt so dashboard can always display it.

**Config:**
```yaml
# docker-compose.yml (relevant env vars)
rag-ingestion:
  environment:
    - EMBED_MODEL=${EMBED_MODEL:-nomic-embed-text}
    - QDRANT_COLLECTION=${RAG_COLLECTION:-documents}
    - CHUNK_SIZE=${RAG_CHUNK_SIZE:-400}
    - CHUNK_OVERLAP=${RAG_CHUNK_OVERLAP:-50}
```

---

## SECTION 4 — Workstreams (Detailed)

### WS1: Unified Model Access

**Status: ✅ Complete (M1 + M3 + M4 + extensions)**

**Provider abstraction (`model-gateway/main.py`):**
- `_model_provider_and_id(name)` → `(provider, model_id)` by prefix
- Ollama: translate to `/api/chat`, `/api/embed`; delta streaming ✓
- vLLM: native OpenAI format; proxy directly ✓
- TTL model list cache (60s default; stale-serve on provider error) ✓
- `DELETE /v1/cache` to invalidate cache on demand ✓
- `X-Request-ID` generated or forwarded on every chat/embeddings call ✓
- Responses API (`/v1/responses`) with tool-call pass-through ✓
- Completions compat (`/v1/completions`) ✓

**Compatibility:**

| Client | Current | Target | Change needed |
|--------|---------|--------|---------------|
| Open WebUI | `OLLAMA_BASE_URL=http://ollama:11434` | `OPENAI_API_BASE=http://model-gateway:11435/v1` | Update compose env + docs |
| OpenClaw | `gateway` provider → `http://model-gateway:11435/v1` ✓ | No change | None |
| N8N | No LLM node set | `OPENAI_API_BASE=http://model-gateway:11435/v1` | Docs only |
| Cursor/external | `http://localhost:11435/v1` | Same | No change |

**OpenClaw-specific (confirmed working):**
- `models.providers.gateway.baseUrl`: `http://model-gateway:11435/v1`
- `models.providers.gateway.api`: `openai-completions`
- `models.providers.gateway.headers.X-Service-Name`: `openclaw`
- Default model: `gateway/ollama/qwen2.5:7b` with `google/gemini-2.0-flash-lite` fallback
- Config sync: `merge_gateway_config.py` adds gateway provider if missing
- **No migration needed** — existing `ollama` provider continues to work; users select provider per model

**vLLM compose profile (optional, future):**
```yaml
# overrides/vllm.yml
services:
  vllm:
    profiles: [vllm]
    image: vllm/vllm-openai:latest
    ports:
      - "8000:8000"
    environment:
      - MODEL=${VLLM_MODEL:-meta-llama/Llama-3.2-3B-Instruct}
    deploy:
      resources:
        limits:
          memory: 16G
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

### WS2: Shared Tools Everywhere (MCP)

**Status: ✅ Complete (M3 + M5)**

**What's implemented:**
- MCP Gateway reads `servers.txt` (10s hot-reload); `mcp/gateway-wrapper.sh` manages startup
- Dashboard reads `servers.txt` (enabled list) and `registry.json` (metadata) to produce enriched tool view
- `GET /api/mcp/servers` — returns `{enabled, catalog, dynamic, registry}`
- `GET /api/mcp/health` — probes each enabled server against MCP gateway; returns `{ok, checked_at}` per server
- Dashboard UI shows health badges per tool
- `filesystem` removed from default `servers.txt`; in registry with `allow_clients: []`
- MCP secrets (`GITHUB_PERSONAL_ACCESS_TOKEN`) passed via compose env from root `.env`

**Current policy model:**
- `allow_clients: ["*"]` = all clients get the tool (default for enabled tools)
- `allow_clients: []` = tool disabled in registry (requires explicit opt-in to enable)
- Per-client enforcement: **not yet implemented** — requires Docker MCP Gateway `X-Client-ID` support (M6)

**OpenClaw-specific:**
- `openclaw-mcp-bridge` plugin → `http://mcp-gateway:8811/mcp` ✓
- Tools surface as `gateway__duckduckgo_search`, etc.
- Future per-agent policy: add `X-Client-ID: openclaw` header; gateway checks `allow_clients`

**Planned (M6):** Auto-disable after 3 consecutive health failures; per-client allowlist enforcement at gateway.

### WS3: Dashboard as Control Center (Ops)

**Status: ✅ Complete (M2 + M5 extensions)**

**Implemented:**
- `ops-controller/main.py`: `verify_token` Depends; `ALLOWED_SERVICES` allowlist; `ConfirmBody(confirm, dry_run)` for all mutating ops; `_audit()` writes JSONL with `correlation_id`
- `dashboard/app.py`: auth middleware on `/api/*` (except health, auth/config, hardware, rag/status); forwards `X-Request-ID` to ops controller
- Hardware stats (`GET /api/hardware`) — CPU, memory, optional nvidia-smi GPU stats
- Default model management (`GET/POST /api/config/default-model`) — updates `DEFAULT_MODEL` env; restarts Open WebUI
- Throughput benchmark (`POST /api/throughput/benchmark`) — token/s measurement against Ollama

**Known limitations:**
- `actor` field in `_audit()` hardcoded to `"dashboard"` — acceptable for now; multi-actor needs identity propagation
- No CSRF token — sufficient for localhost deployment

**Improvement for `_audit()`:**
```python
# ops-controller/main.py — add metadata support
def _audit(action, resource="", result="ok", detail="", correlation_id="", metadata=None):
    entry = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "action": action,
        "resource": resource,
        "actor": "dashboard",
        "result": result,
        "detail": detail or "",
    }
    if correlation_id:
        entry["correlation_id"] = correlation_id
    if metadata:
        entry["metadata"] = metadata
    # ...

# In service_logs:
_audit("logs", service_id, "ok", metadata={"tail": min(tail, 500)})
```

### WS4: Auditing, Security, and Threat Model

**Threat model table:**

| Asset | Threat | Current State | Mitigation |
|-------|--------|---------------|------------|
| `docker.sock` (ops-controller) | Container escape → host RCE | Mounted; allowlisted actions only | Token auth; no host port; allowlist in code; document: run ops-controller read-only workspace mount |
| `docker.sock` (mcp-gateway) | MCP server escapes → host pivot | Mounted; Docker MCP Gateway owns it | Accept: required for spawning server containers; isolate mcp-gateway to backend network |
| Ops controller token | Token theft → privileged ops | Token in `.env`; no default | Generate with `openssl rand -hex 32`; never expose controller port to host |
| MCP tools (filesystem) | Data exfiltration via tool | Enabled in servers.txt; broken without root-dir | Remove from default servers.txt; require explicit opt-in |
| MCP tools (browser/playwright) | SSRF → RFC1918/metadata | No egress blocks yet | Add `DOCKER-USER` iptables egress block; document in runbooks |
| Tool output → model | Prompt injection via tool output | No sandbox; tool output passed to model | Allowlists; structured tool calls (tool output in `<tool_result>` tags); validate tool schemas |
| Dashboard auth | Unauthenticated admin | Optional `DASHBOARD_AUTH_TOKEN` | Document: set for networked use; pre-deployment checklist item |
| `openclaw.json` plaintext keys | Key exposure if file shared/backed up | In gitignored `data/`; acceptable on local disk | Flag in docs: avoid including `data/openclaw/` in cloud backups without encryption |
| WEBUI_AUTH=False | Open WebUI accessible without auth | Explicit in compose env | Change default to `WEBUI_AUTH=${WEBUI_AUTH:-True}`; opt-out, not opt-in |
| Model gateway | No auth on `/v1/` endpoints | None; local-first intentional | Acceptable for localhost; add API key support if exposed to LAN |

**AuthN/AuthZ approach:**
- **Tier 0:** No auth (health endpoints, read-only model list)
- **Tier 1:** Bearer token (ops controller — `OPS_CONTROLLER_TOKEN`; optional dashboard — `DASHBOARD_AUTH_TOKEN`)
- **Future Tier 3:** OAuth / OIDC (if multi-user or Tailscale integration needed)
- **RBAC:** Currently binary (authed = full access). Future: read-only role (view logs, health) vs admin role (start/stop).

**Audit event schema (full):**
```json
{
  "ts": "2026-03-01T12:34:56.789Z",
  "action": "restart",
  "resource": "ollama",
  "actor": "dashboard",
  "result": "ok",
  "detail": "",
  "correlation_id": "req-abc123",
  "metadata": {"dry_run": false}
}
```

Fields:
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ts` | string | Yes | ISO8601 UTC |
| `action` | enum | Yes | `start\|stop\|restart\|pull\|logs\|mcp_add\|mcp_remove\|model_pull\|model_delete` |
| `resource` | string | Yes | Service ID, model name, or tool name |
| `actor` | string | Yes | `dashboard\|cli\|api` |
| `result` | enum | Yes | `ok\|error` |
| `detail` | string | No | Error message or context |
| `correlation_id` | string | No | From `X-Request-ID` header |
| `metadata` | object | No | Extra context (tail count, dry_run, etc.) |

**Correlation ID flow:**
1. External client sends `X-Request-ID: req-abc` to model gateway
2. Model gateway logs it; includes in throughput record to dashboard
3. Dashboard passes `X-Request-ID` when calling ops controller
4. Ops controller includes in audit entry
5. Result: one request traceable across model → throughput → ops → audit

**Secret handling end-to-end:**
- `.env` — gitignored, host-only, not committed ✓
- `mcp/.env` — gitignored, host-only; mount as Docker secret via compose `secrets:` block
- `data/openclaw/openclaw.json` — gitignored; contains Telegram token, skill API key, gateway auth token. **Recommendation:** Move sensitive values to `.env` and reference via compose `env_file:`. The `merge_gateway_config.py` can inject from env.
- Gateway tokens — in `.env`, set via compose `environment:` ✓
- **Secret rotation:** Update `.env`, `docker compose up -d --force-recreate <service>`. Document in `BACKUP_RESTORE.md`.

**SSRF defenses (MCP):**
```bash
# Add to host firewall (iptables) or docker-compose DOCKER-USER chain
# Block MCP containers from reaching RFC1918 + metadata endpoints
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 10.0.0.0/8 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 172.16.0.0/12 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 192.168.0.0/16 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 100.64.0.0/10 -j DROP
iptables -I DOCKER-USER -s <mcp_gateway_subnet> -d 169.254.169.254/32 -j DROP
```
SSRF scripts live at `scripts/ssrf-egress-block.sh` (Linux/WSL2) and `scripts/ssrf-egress-block.ps1` (Windows guidance). Runbook: `docs/runbooks/SECURITY_HARDENING.md`.

### WS5: Best-in-Class Docker/Compose & Repo Organization

**Status: ✅ Complete (M3 + M4)**

**Compose hardening — confirmed current state (`docker-compose.yml`):**

| Check | Status |
|-------|--------|
| Non-root | `model-gateway`, `dashboard`, `n8n`: `user: "1000:1000"` ✓ |
| `cap_drop: [ALL]` | `model-gateway`, `dashboard`, `ops-controller` ✓ |
| `security_opt: [no-new-privileges:true]` | `model-gateway`, `dashboard`, `ops-controller` ✓ |
| `read_only: true` + `tmpfs: [/tmp]` | `model-gateway`, `dashboard` ✓ |
| Healthchecks | All long-running services including `openclaw-gateway` ✓ |
| Resource limits | All services including `openclaw-gateway` (2G), `qdrant` (512M), `rag-ingestion` (256M) ✓ |
| Log rotation | All services including `n8n`, `comfyui`, `openclaw-gateway`, `qdrant`, `rag-ingestion` ✓ |
| Pinned images | `ollama:0.17.4`, `open-webui:v0.8.4`, `curlimages/curl:8.10.1`, `python:3.12.8-slim`, `qdrant:v1.13.4` ✓ |
| Explicit networks | `ai-toolkit-frontend`, `ai-toolkit-backend` declared; Ollama backend-only ✓ |
| Named volumes | Bind mounts used (intentional for local-first; backup documented) ✓ |
| `restart: unless-stopped` | All long-running services ✓ |
| One-shot `restart: "no"` | pullers, sync services ✓ |

**Remaining (M6):**
- `mcp-gateway`: currently on `frontend` network (has host port); move to backend-only
- `WEBUI_AUTH` default: change from `False` to `True`

### WS6: RAG Pipeline

**Status: ✅ Complete (M5-ext)**

**What's implemented:**
- `qdrant` service — vector DB, backend-only, `:6333` (no direct user access needed)
- `rag-ingestion` service — `--profile rag`; watches `data/rag-input/`; chunks → embeds via model gateway → stores in Qdrant
- Open WebUI — `VECTOR_DB=qdrant`, `QDRANT_URI=http://qdrant:6333`; RAG search in chat UI
- `GET /api/rag/status` — auth-exempt; returns collection status and point count

**User flow:**
```
1. ./compose --profile rag up -d          # start Qdrant + rag-ingestion
2. cp document.pdf data/rag-input/        # drop document
3. rag-ingestion chunks + embeds + stores # automatic
4. Open WebUI chat → toggle RAG           # retrieves relevant chunks
```

**Configuration:**
- Embed model: `EMBED_MODEL` (default `nomic-embed-text`) — must be pulled first
- Chunk size: `RAG_CHUNK_SIZE` (default 400 tokens, overlap 50)
- Collection: `RAG_COLLECTION` (default `documents`)

**Planned (M6):**
- Add `nomic-embed-text` to `model-puller` default model list
- Document RAG setup in `GETTING_STARTED.md`
- Add `test_rag_ingestion.py` contract test

**Network assignment (current):**

| Service | Frontend | Backend | Notes |
|---------|----------|---------|-------|
| open-webui | ✓ | ✓ | Needs model-gateway, qdrant |
| dashboard | ✓ | ✓ | Needs ollama, ops-controller, mcp-gateway |
| n8n | ✓ | — | |
| openclaw-gateway | ✓ | ✓ | Needs model-gateway, mcp-gateway |
| model-gateway | ✓ | ✓ | Frontend for external clients; backend for Ollama |
| mcp-gateway | ✓ | — | Has host port `:8811`; M6: move to backend-only |
| ops-controller | — | ✓ | Internal only; no host port |
| ollama | — | ✓ | Backend-only by default; `overrides/ollama-expose.yml` for Cursor |
| qdrant | — | ✓ | Backend-only; no host port needed for compose services |
| comfyui | ✓ | — | |
| rag-ingestion | — | ✓ | Backend-only; no ingress needed |

**Repo structure (current):**
```
LLM-toolkit/
├── dashboard/           ✓ exists
├── model-gateway/       ✓ exists
├── ops-controller/      ✓ exists
├── mcp/                 ✓ (Dockerfile, gateway-wrapper.sh, registry.json.example, README.md)
├── openclaw/            ✓ (workspace/, scripts/, openclaw.json.example)
├── rag-ingestion/       ✓ (Dockerfile, ingest.py, requirements.txt)
├── scripts/             ✓ (detect_hardware.py, ssrf-egress-block.sh/.ps1, mcp_add/remove.sh/.ps1, smoke_test.sh/.ps1, comfyui/)
├── tests/               ✓ (test_compose_smoke.py, test_dashboard_health.py, test_mcp_policy.py, test_model_gateway_cache.py, test_model_gateway_contract.py, test_ops_controller_audit.py)
├── docs/
│   ├── Product Requirements Document.md  ✓ this file (architecture & decisions)
│   ├── GETTING_STARTED.md     ✓
│   ├── audit/SCHEMA.md        ✓
│   └── runbooks/
│       ├── TROUBLESHOOTING.md ✓
│       ├── BACKUP_RESTORE.md  ✓
│       ├── UPGRADE.md         ✓
│       └── SECURITY_HARDENING.md  ✓ (SSRF rules, iptables, token rotation)
├── data/                # gitignored, runtime data
│   ├── mcp/
│   │   ├── servers.txt  ✓
│   │   └── registry.json  ✓ (created from registry.json.example)
│   ├── ops-controller/
│   │   └── audit.log    # runtime; grows unbounded (M6: add rotation)
│   ├── qdrant/          # Qdrant vector DB storage
│   ├── rag-input/       # Drop documents here for ingestion (--profile rag)
│   └── openclaw/        # OpenClaw config + workspace (gitignored)
├── docker-compose.yml   ✓
├── compose               # Helper script (auto-detects hardware, wraps docker compose)
├── overrides/           # Optional compose overrides
│   ├── compute.yml      # Auto-generated by detect_hardware.py; gitignored
│   ├── openclaw-secure.yml   # Bind OpenClaw to localhost only
│   ├── ollama-expose.yml     # Expose Ollama host port (Cursor, CLI)
│   └── vllm.yml              # vLLM provider profile (--profile vllm)
├── .env.example         ✓ (ensure RAG vars added: EMBED_MODEL, RAG_COLLECTION, QDRANT_PORT)
├── .env                 # gitignored ✓
└── SECURITY.md          ✓
```

---

## SECTION 5 — Implementation Plan

### Milestones

| Milestone | Status | User-visible Outcomes |
|-----------|--------|----------------------|
| **M0** | ✅ Done | Audit schema, Docker healthchecks, log rotation, SECURITY.md, runbooks |
| **M1** | ✅ Done | Model Gateway: OpenAI-compat, Ollama+vLLM, streaming, embeddings, throughput |
| **M2** | ✅ Done | Ops Controller: start/stop/restart/logs/pull/audit; dashboard calls controller; bearer auth |
| **M3** | ✅ Done | MCP registry.json + health API; cap_drop/read_only hardening; model list cache; Open WebUI → gateway default |
| **M4** | ✅ Done | Explicit Docker networks (frontend/backend); correlation IDs (X-Request-ID → audit); vLLM compose profile; smoke tests |
| **M5** | ✅ Done | Dashboard MCP health dots (green/yellow/red); SSRF egress scripts; hardware stats; throughput benchmark; default-model management |
| **M5-ext** | ✅ Done | RAG pipeline (Qdrant + rag-ingestion); Open WebUI → Qdrant; RAG status endpoint; Responses API + completions compat; cache-bust endpoint; openclaw-cli profile |
| **M6** | 🔲 Planned | `WEBUI_AUTH` default → True; mcp-gateway backend-only; CI pipeline; MCP per-client policy; audit log rotation; openclaw.json token externalization |
| **M7** | 🔲 Planned | **Reliability spine** (Section 10): dependency registry + typed readiness; L1–L3 health; timeouts/retry policy; model-gateway fallbacks + warmup/streaming/schema hardening; MCP circuit breakers + schema validation + execution classes; browser session lifecycle; golden trace + failure taxonomy + SLO-style reporting; stack “doctor” / config validation |

---

### M3 — MCP Health + Compose Hardening + Model Cache ✅ (Done)

**User-visible outcomes:**
- Dashboard shows green/yellow/red health badge per MCP tool
- `filesystem` no longer silently broken by default
- Model list loads faster (cached); gateway survives Ollama brief downtime
- Open WebUI defaults to gateway endpoint (models from all providers visible)

**PR slices:**

**PR3-A: MCP registry.json + dashboard health**
- `data/mcp/registry.json` — create with schema above; include `filesystem: allow_clients: []`
- `data/mcp/servers.txt` — remove `filesystem` from default
- `dashboard/app.py` — add `GET /api/mcp/health`; read registry.json for metadata enrichment; update `GET /api/mcp/servers` to merge servers.txt + registry
- `dashboard/static/` — add health badges per tool in MCP panel
- `tests/test_dashboard_mcp_health.py` — contract test for health endpoint

Acceptance criteria:
- **Given** `duckduckgo` in `servers.txt`, **When** `GET /api/mcp/health`, **Then** response contains `{"health": {"duckduckgo": {"ok": bool, "checked_at": "..."}}}` 
- **Given** `filesystem` not in `servers.txt`, **When** dashboard loads MCP section, **Then** no error about filesystem

**PR3-B: Compose hardening (cap_drop + read_only)**
- `docker-compose.yml` — add `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]` to `model-gateway`, `dashboard`, `ops-controller`
- `docker-compose.yml` — add `read_only: true` + `tmpfs: [/tmp]` to `model-gateway` and `dashboard`
- `docker-compose.yml` — add log rotation to `n8n` and `comfyui`
- `docker-compose.yml` — add resource limits to `openclaw-gateway`
- `docker-compose.yml` — add healthcheck to `openclaw-gateway`
- `docker-compose.yml` — change Open WebUI: `WEBUI_AUTH=${WEBUI_AUTH:-True}`, add `OPENAI_API_BASE`

Acceptance criteria:
- **Given** `docker compose up -d`, **When** `docker inspect model-gateway`, **Then** `HostConfig.CapDrop` contains `ALL`, `ReadonlyRootfs` is `true`
- **Given** `docker compose up -d`, **When** `docker compose ps`, **Then** all services `healthy` within 2 minutes

**PR3-C: Model gateway caching + correlation IDs**
- `model-gateway/main.py` — add TTL cache for `/v1/models` (60s default, `MODEL_CACHE_TTL_SEC` env)
- `model-gateway/main.py` — generate/propagate `X-Request-ID` in chat completions and embeddings
- `docker-compose.yml` — add `MODEL_CACHE_TTL_SEC=60` to model-gateway env

Acceptance criteria:
- **Given** two consecutive `GET /v1/models` within 60s, **When** Ollama is stopped between them, **Then** second call returns cached data successfully
- **Given** `POST /v1/chat/completions` with `X-Request-ID: req-test`, **When** request completes, **Then** response headers contain `X-Request-ID: req-test`

File-level changes:
| File | Change |
|------|--------|
| `model-gateway/main.py` | Add TTL cache, `X-Request-ID` propagation |
| `dashboard/app.py` | Add `GET /api/mcp/health`, enrich MCP servers with registry metadata |
| `docker-compose.yml` | cap_drop, security_opt, read_only, WEBUI_AUTH, openclaw limits/healthcheck, n8n/comfyui log rotation |
| `data/mcp/registry.json` | Create with full schema |
| `data/mcp/servers.txt` | Remove `filesystem` from default |
| `tests/test_dashboard_mcp_health.py` | Contract test for health endpoint |
| `tests/test_model_gateway_cache.py` | Test TTL cache behavior |

Security/audit checklist for M3:
- [ ] `cap_drop: [ALL]` verified on model-gateway, dashboard, ops-controller
- [ ] `read_only: true` verified on model-gateway, dashboard
- [ ] `WEBUI_AUTH=True` is now default (users can opt out with `WEBUI_AUTH=False`)
- [ ] `filesystem` removed from default servers.txt
- [ ] No new secrets introduced
- [ ] Contract tests pass

---

### M4 — Networks + Correlation + vLLM + Smoke Tests ✅ (Done)

**User-visible outcomes (implemented):**
- Explicit `ai-toolkit-frontend` / `ai-toolkit-backend` networks; services assigned; Ollama/ops-controller on backend only
- Request IDs: `X-Request-ID` forwarded dashboard → ops-controller and stored in audit entries; `datetime.now(timezone.utc)` in audit
- vLLM: `overrides/vllm.yml` with profile `vllm`; GETTING_STARTED.md updated
- Smoke tests: `tests/test_compose_smoke.py` (config valid, networks present, vllm override valid; optional `RUN_COMPOSE_SMOKE=1` runtime check)
- SSRF egress blocks: documented in `docs/runbooks/SECURITY_HARDENING.md` (manual iptables); no automated script yet (M5)

**PR slices (completed):**

**PR4-A: Explicit Docker networks**
- `docker-compose.yml` — declare `networks:` section; assign services to frontend/backend
- `docs/runbooks/SECURITY_HARDENING.md` — document SSRF egress iptables rules

**PR4-B: Correlation ID end-to-end**
- `ops-controller/main.py` — accept `X-Request-ID` header in all endpoints; pass to `_audit()`
- `dashboard/app.py` — forward `X-Request-ID` when calling ops controller

**PR4-C: vLLM optional profile**
- `overrides/vllm.yml` — vLLM service, `profiles: [vllm]`
- `model-gateway/main.py` — already supports vLLM via `VLLM_URL` env ✓
- `docs/` — add vLLM setup guide

**PR4-D: Compose smoke test** ✅
- `tests/test_compose_smoke.py` — config validation + optional `RUN_COMPOSE_SMOKE=1` runtime smoke

---

### M5 — ✅ Complete

- **Dashboard UI:** MCP health dots (green/yellow/red) per tool; gateway badge "gateway ok" / "gateway unreachable".
- **SSRF scripts:** `scripts/ssrf-egress-block.sh` (Linux/WSL2) — auto-detect subnet, `--dry-run` / `--remove`; `scripts/ssrf-egress-block.ps1` (Windows guidance). Runbook updated.
- **Hardware stats:** `GET /api/hardware` — CPU, memory, optional GPU stats.
- **Throughput benchmark:** `POST /api/throughput/benchmark` — token/s measurement.
- **Default model management:** `GET/POST /api/config/default-model` — set default; restarts Open WebUI.

### M5-ext — ✅ Complete (Extended deliverables)

- **RAG pipeline:** Qdrant service (backend-only, `:6333`); `rag-ingestion` watch-mode ingester (`--profile rag`); Open WebUI → Qdrant (`VECTOR_DB=qdrant`); `GET /api/rag/status` in dashboard.
- **Responses API:** `/v1/responses` — OpenAI Responses API format; converts to chat completions; tool-call pass-through; streaming ✓.
- **Completions compat:** `/v1/completions` — legacy completions endpoint wrapping chat completions.
- **Cache invalidation:** `DELETE /v1/cache` — force model list re-fetch.
- **OpenClaw CLI profile:** `--profile openclaw-cli` — interactive CLI with gateway token only; no session credentials.
- **Ollama backend-only:** Ollama no longer exposes host port by default; use `overrides/ollama-expose.yml` for Cursor/external access.

---

### M6 — Planned

**Priority items:**

| Item | Rationale | Effort |
|------|-----------|--------|
| `WEBUI_AUTH` default → `True` | Security: Open WebUI currently ships open | XS — 1-line compose change + UPGRADE.md note |
| mcp-gateway → backend network only | Reduce attack surface; internal services don't need host port | S |
| CI pipeline (GitHub Actions) | Run compose smoke tests + contract tests on push | M |
| Audit log rotation | `data/ops-controller/audit.log` grows unbounded; add in-process rotation at 10MB | S |
| MCP per-client policy enforcement | `allow_clients` currently metadata-only; needs Docker MCP Gateway `X-Client-ID` support | L (external dep) |
| openclaw.json token externalization | Move Telegram token + skill API keys from JSON to `.env` via `merge_gateway_config.py` | M |
| RBAC (read-only role) | View logs/health without start/stop access | L |

### M7 — Reliability spine (planned — see Section 10)

**Outcome:** When OpenClaw or any client fails, operators can tell **which hop** failed (OpenClaw vs model gateway vs Ollama/vLLM vs MCP vs browser bridge) and whether the failure is **retryable** or **operator-action-required**.

**Phase 1 (failures visible):** Typed `/health` and `/ready` (or equivalent) for model gateway, MCP gateway, and browser bridge; **dependency registry** in config + dashboard surface; **`X-Request-ID` / correlation** end-to-end; **failure taxonomy**; dashboard dependency status; **OpenClaw startup validation** (bridge plugin, gateway URL, token, default model); **smoke tests**: OpenClaw → model gateway → inference; OpenClaw → MCP → sample tool.

**Phase 2 (degradation & recovery):** Provider **fallback chains** in model gateway; **per-tool / per-server circuit breakers** in MCP path; cold/warm model state in health; **standardized timeout & retry budgets**; auto-disable/quarantine unhealthy tools after threshold; **ops-controller** restart hooks (recovery only); browser bridge **session health / recycle**.

**Phase 3 (operator-grade):** SLO dashboard; version-pinned bundles; rollback; `BASE_PATH` backup/restore; config migration engine; expanded integration test matrix.

**Explicit non-goals:** Monolithic OpenClaw fork; dashboard as **required** runtime dependency for normal requests; ops-controller in the **hot path** for chat/tool calls; new services before contracts harden; “restart fixes it” as primary strategy.

---

## SECTION 6 — "First PR" (Do Now — M6)

All M0–M5 items are shipped. The highest-value, lowest-risk M6 items are:

**PR6-A: `WEBUI_AUTH` default + mcp-gateway network**

1. Change `WEBUI_AUTH=${WEBUI_AUTH:-False}` → `WEBUI_AUTH=${WEBUI_AUTH:-True}` in `open-webui` env
2. Move `mcp-gateway` to `backend` network only (remove from `frontend`; remove host port from default compose; document in `overrides/mcp-expose.yml` if needed)

None of these break existing functionality for users who set env vars explicitly. Document in `UPGRADE.md`.

### M6 Steps

**Step 1: `WEBUI_AUTH` default**

In `docker-compose.yml` `open-webui.environment`:
```yaml
      - WEBUI_AUTH=${WEBUI_AUTH:-True}   # was False
```

Document in `UPGRADE.md`: users who want single-user open mode set `WEBUI_AUTH=False` in `.env`.

**Step 2: mcp-gateway network isolation**

```yaml
# docker-compose.yml — mcp-gateway
    # Remove from frontend network; internal only
    networks:
      - backend

# If external MCP access is needed, create overrides/mcp-expose.yml
```

**Step 3: Audit log in-process rotation**

In `ops-controller/main.py`, add log rotation at 10MB:
```python
import os
AUDIT_LOG_PATH = os.environ.get("AUDIT_LOG_PATH", "/data/audit.log")
MAX_AUDIT_BYTES = 10 * 1024 * 1024  # 10MB

def _audit(action, ...):
    # rotate if needed
    if os.path.exists(AUDIT_LOG_PATH) and os.path.getsize(AUDIT_LOG_PATH) > MAX_AUDIT_BYTES:
        os.rename(AUDIT_LOG_PATH, AUDIT_LOG_PATH + ".1")
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
```

**Step 4: CI pipeline**

`.github/workflows/test.yml`:
```yaml
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -r tests/requirements.txt
      - run: python -m pytest tests/ -v --ignore=tests/test_compose_smoke.py
```

### M6 Acceptance Criteria

- **Given** `docker compose up -d`, **When** env does not set `WEBUI_AUTH`, **Then** Open WebUI requires login
- **Given** `docker inspect mcp-gateway`, **Then** `NetworkSettings.Networks` contains only `ai-toolkit-backend`
- **Given** audit log exceeds 10MB, **When** next privileged action occurs, **Then** old log renamed to `audit.log.1` and new log started
- **Given** push to main branch, **When** CI runs, **Then** all contract + smoke tests pass

### Test plan (current, before M6)

```bash
# Unit/contract tests
python -m pytest tests/ -v

# Compose smoke
./compose up -d
docker compose ps           # all services healthy within 3 min
curl -s http://localhost:11435/v1/models | jq .data[].id
curl -s http://localhost:8080/api/mcp/health | jq .health
curl -s http://localhost:8080/api/rag/status | jq .
docker inspect $(docker compose ps -q model-gateway) --format '{{.HostConfig.CapDrop}}'
# → [ALL]
```

---

## SECTION 7 — Risks & Mitigations

| Risk | Impact | Mitigation | Rollback |
|------|--------|------------|---------|
| `read_only: true` breaks model-gateway or dashboard | Service crash if writes to unexpected paths | Add `tmpfs: [/tmp]`; test with `docker compose up` before merging; check for writes in `/app` | Remove `read_only: true` from affected service |
| `cap_drop: [ALL]` breaks N8N or ComfyUI | Service fails if needing capabilities | Apply to custom-build services first; test third-party (n8n, comfyui) separately; add `cap_add: [CHOWN, SETUID, SETGID]` as needed | Remove `cap_drop` from affected service |
| ops-controller user change breaks docker.sock access | 403 on all docker operations | Verify docker group GID on host: `stat -c %g /var/run/docker.sock`; set `user: "1000:<gid>"` | Revert user to root temporarily |
| Model gateway cache serves stale model list | Users see models that were deleted from Ollama | Cache TTL is 60s (short); `DELETE /v1/cache` endpoint to invalidate (add in M4) | Set `MODEL_CACHE_TTL_SEC=0` in `.env` to disable cache |
| WEBUI_AUTH=True breaks existing setups | Users locked out of Open WebUI | Document the change in UPGRADE.md; users set `WEBUI_AUTH=False` to opt out | `WEBUI_AUTH=False` in `.env` |
| docker.sock in two services | Two attack surfaces for container escape | Accept: both required (MCP needs to spawn servers; ops needs lifecycle control). Mitigate with allowlists, auth, no host ports. | Remove one; document trade-off |
| MCP filesystem SSRF | Tool access to host filesystem | Removed from default; `allow_clients: []` in registry; require explicit opt-in | Clear from servers.txt |
| Prompt injection via MCP tool output | Model manipulated by tool results | Allowlists (only trusted tools enabled); structured output in tool_result tags; monitor model behavior | Remove suspicious tool from servers.txt |
| openclaw.json plaintext tokens on disk | Local token exposure if data/ is shared | Tokens are in gitignored `data/`; document: do not include data/openclaw/ in unencrypted cloud backups | Rotate tokens; regenerate with openssl |
| Performance regression from gateway proxy | >10ms added latency | Gateway is thin async proxy; benchmarked acceptable. Cache reduces model-list overhead | Direct `OLLAMA_BASE_URL` escape hatch for any service |

---

## SECTION 8 — Open Questions

| # | Question | Status |
|---|----------|--------|
| 1 | **Ops-controller docker GID:** `user: "1000:<gid>"` value for ops-controller/mcp-gateway depends on host docker GID | ✅ Resolved — ops-controller runs without explicit user (docker.sock access via root-equiv); acceptable since no host port |
| 2 | **Open WebUI `OPENAI_API_BASE`:** Does `open-webui:v0.8.4` support this env? | ✅ Resolved — uses `OPENAI_API_BASE_URL`; working in compose |
| 3 | **MCP gateway policy:** Does Docker MCP Gateway support `X-Client-ID` header for per-client allowlist enforcement? | 🔲 Open — not yet; per-client policy deferred to M6 |
| 4 | **openclaw.json token externalization:** Can `merge_gateway_config.py` inject tokens from env? | 🔲 Open — planned for M6 |
| 5 | **Ollama host port:** Remove to reduce attack surface? | ✅ Resolved — Ollama is backend-only by default; `overrides/ollama-expose.yml` for Cursor/CLI |
| 6 | **Audit log rotation:** `audit.log` grows unbounded | 🔲 Open — in-process rotation at 10MB planned for M6 |
| 7 | **vLLM timing** | ✅ Resolved — `overrides/vllm.yml` with `--profile vllm`; available now |
| 8 | **ComfyUI non-root** | 🔲 Open — `yanwk/comfyui-boot:cpu` runs as root; image limitation; acceptable for now |
| 9 | **Smoke test in CI** | 🔲 Open — no CI pipeline yet; M6 item |
| 10 | **N8N LLM node** | 🔲 Open — use OpenAI-compat node with `baseURL: http://model-gateway:11435/v1`; needs example workflow doc |
| 11 | **RAG embed model pull** | 🔲 Open — `nomic-embed-text` must be pulled before `rag-ingestion` can embed; add to model-puller default list or document in GETTING_STARTED |
| 12 | **Reliability spine (M7):** dependency registry schema, `/ready` vs `/health` split, circuit-breaker semantics | 🔲 Open — requirements in Section 10; APIs TBD |

---

## SECTION 9 — OpenClaw Trust Model: Orchestrator / Browser Paradigm

This section formalises the security stance for OpenClaw and any future agentic runtimes in the
stack. The model mirrors Anthropic's own agent safety guidance: treat the environment as untrusted,
separate credential-holding processes from action-taking processes.

### 9.1 The Two-Tier Model

```
┌─────────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR TIER                                              │
│  openclaw-gateway                                               │
│  • Holds all session credentials (CLAUDE_*_SESSION_KEY, etc.)  │
│  • Holds openclaw.json (Telegram token, skill API keys)         │
│  • Directs tool calls and model calls                           │
│  • Trusts tool outputs structurally, not verbatim               │
└──────────────────────────────┬──────────────────────────────────┘
                               │ gateway token only (OPENCLAW_GATEWAY_TOKEN)
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  BROWSER / WORKER TIER                                          │
│  openclaw-cli  (and future: openclaw-browser)                   │
│  • Holds gateway token ONLY — zero session credentials          │
│  • Workspace files: read-only                                   │
│  • Config dir (openclaw.json): NOT mounted                      │
│  • Egress to RFC1918 / metadata blocked (see §9.4)              │
└─────────────────────────────────────────────────────────────────┘
```

**Core invariants:**
1. **No credentials in the browser tier.** A compromised or prompt-injected worker cannot exfiltrate Claude/Anthropic session tokens.
2. **Config is read-only or absent in the browser tier.** `openclaw.json` (which contains Telegram tokens and skill API keys) is mounted only in the orchestrator container.
3. **Workspace is read-only in the browser tier.** Workers can read workspace files; only the orchestrator writes them.
4. **Egress from browser-tier containers is blocked to RFC1918 + metadata endpoints** to prevent SSRF pivoting to internal services.

### 9.2 Container Trust Tier Map

| Container | Tier | Session Credentials | openclaw.json | Workspace | Egress |
|-----------|------|---------------------|---------------|-----------|--------|
| `openclaw-gateway` | Orchestrator | ✓ All (`CLAUDE_*`) | ✓ Read-write | ✓ Read-write | Allowed (needs model-gateway, mcp) |
| `openclaw-cli` | Browser-tier | ✗ None | ✗ Not mounted | Read-only | RFC1918 blocked (§9.4) |
| `openclaw-browser` *(future)* | Browser-tier | ✗ None | ✗ Not mounted | ✗ Not mounted | RFC1918 + metadata blocked |

### 9.3 Container Hardening (both tiers)

Both containers run with:
```yaml
cap_drop: [ALL]
security_opt: ["no-new-privileges:true"]
```

`openclaw-gateway` additionally has:
```yaml
deploy:
  resources:
    limits:
      memory: 2G
healthcheck:
  test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://localhost:6680"]
  start_period: 60s
  interval: 30s
  timeout: 10s
  retries: 3
```

`openclaw-cli` has `restart: "no"` because it is an interactive/on-demand process; it must not
restart automatically and re-acquire a token without user intent.

### 9.4 Egress Control for Browser-Tier Containers

When a browser/playwright feature is active, the worker container can make arbitrary outbound HTTP
requests. Without egress controls, a malicious page or prompt injection can reach internal services
(Ollama, ops-controller, cloud metadata).

Apply RFC1918 + metadata blocks via `scripts/ssrf-egress-block.sh`:

```bash
# Block the openclaw network specifically (auto-detects ai-toolkit-openclaw subnet):
./scripts/ssrf-egress-block.sh --target openclaw

# Block both MCP and openclaw in one pass:
./scripts/ssrf-egress-block.sh --target all
```

The script blocks:
- `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` (RFC1918)
- `100.64.0.0/10` (Tailscale CGNAT)
- `169.254.169.254/32`, `169.254.170.2/32` (cloud metadata)

DNS (port 53) is explicitly allowed so external hostnames still resolve.

### 9.5 Prompt Injection Defense at the Tool-Output Boundary

Tool outputs returned from browser or MCP calls flow back to the orchestrator as context. To
prevent injected instructions from escalating privileges:

- Tool results are returned in a structured `<tool_result>` boundary by the MCP bridge plugin,
  keeping them separate from the system prompt and user message context.
- The orchestrator must treat tool output as **data**, not as **instructions**.
- Validate tool output schemas where possible (see MCP `registry.json` `outputSchema` field).
- If a tool result contains instruction-like text (e.g. `Ignore previous instructions…`), the
  structured boundary ensures the model can distinguish it from a genuine user or system prompt.

### 9.6 Secret Handling Summary (OpenClaw-specific)

| Secret | Location | Injected by | Notes |
|--------|----------|-------------|-------|
| `OPENCLAW_GATEWAY_TOKEN` | `.env` | Compose `environment:` | Orchestrator + CLI (bridge auth only) |
| `CLAUDE_AI_SESSION_KEY` | `.env` | Compose `environment:` (gateway only) | Never in CLI container |
| `CLAUDE_WEB_SESSION_KEY` | `.env` | Compose `environment:` (gateway only) | Never in CLI container |
| `CLAUDE_WEB_COOKIE` | `.env` | Compose `environment:` (gateway only) | Never in CLI container |
| Telegram bot token | `data/openclaw/openclaw.json` | OpenClaw config sync | Gitignored; do not include in unencrypted cloud backups |
| Skill API keys | `data/openclaw/openclaw.json` | OpenClaw config sync | Same as above |

**Rotation:** See `docs/runbooks/SECURITY_HARDENING.md` §5 (token rotation) and §11 (openclaw secrets).

---

## SECTION 10 — Reliability Layer & Service Contracts (Strategic)

This section captures the **developer/operator view** of what AI-toolkit needs next to be **operationally strong** around **OpenClaw hitting the rest of the stack**. It is **product requirements**, not an implementation spec; APIs and payloads will be designed in follow-on engineering plans.

### 10.1 Positioning: OpenClaw as mesh client

OpenClaw is **not** the center of the architecture; it is **one consumer** of:

- **Model Gateway** (`:11435`) — single OpenAI-compatible surface to Ollama / vLLM.
- **MCP Gateway** (`:8811`) — shared tools via `openclaw-mcp-bridge` and other clients.
- **Browser / CDP bridges** — optional capability; easy to misconfigure (e.g. bridge port vs UI port).

**Effective paths (simplified):**

```
User → OpenClaw UI/Gateway → Model Gateway → Ollama / vLLM
User → OpenClaw UI/Gateway → OpenClaw MCP bridge → MCP Gateway → tool servers
```

Reliability is constrained by **every hop**: model list, inference, tool enumeration, tool execution, and browser sessions. Weak **contracts** anywhere surface as “the assistant is flaky.”

### 10.2 What “very strong” means for this repo

All of the following should eventually hold:

1. **Preflight** — OpenClaw (and tooling) knows whether downstream services are **healthy** before attempting dependent actions.
2. **Isolation** — One bad MCP server, one model timeout, or one broken bridge does **not** poison all tool or model usage.
3. **Traceability** — Every user-visible failure is attributable to a **layer** (OpenClaw, model gateway, provider, MCP gateway, tool server, browser bridge).
4. **Recovery** — **Automatic** retry/backoff for transient failures; **explicit** operator steps for auth/config/provider-down cases.
5. **Versioned contracts** — Config and service expectations are **validated** so semantics do not silently drift.
6. **Security ↔ reliability** — Guardrails (auth, policy, SSRF) **do not** break legitimate recovery paths; reliability mechanisms **do not** weaken security.

**Existing ingredients (to build on):** single model endpoint, MCP gateway, ops controller, dashboard, structured logging, health concepts, `X-Request-ID` propagation (see Section 1, WS1–WS3).

### 10.3 The gap: first-class service contract layer

Documentation today describes **ports and intent**; a **runtime contract** between OpenClaw and dependencies is not yet first-class. “Very strong” requires:

**A. Explicit readiness states (machine-readable)**  
Not only “container running.” Callers need signals such as: model gateway can answer **now**; which providers are live; default model; warm vs cold hints; MCP gateway reachable; which tools/servers are healthy; browser bridge reachable; whether failure is **retryable** vs **operator required**.

**B. Consume dependency state before action selection**  
Policy examples: do not start a model call if no live provider; do not invoke a degraded tool without surfacing degradation; do not assume browser actions if the bridge is down; warn when starting long flows in a degraded state.

### 10.4 Reliability spine (building blocks)

**1) Dependency registry (canonical)**  
One registry listing every runtime dependency OpenClaw (and optionally other clients) rely on: model gateway, backends (Ollama/vLLM), MCP gateway, MCP servers/tools, browser bridge, optional RAG, optional ops controller (as **observer/recovery**, not hot path).

Per dependency (conceptual fields): name, endpoint, auth mode, health endpoint(s), version, timeout budget, retry policy, circuit-breaker policy, fallback target, last healthy timestamp, degraded reason. **Rendered in dashboard** and **consumed** by gateways/agents where appropriate.

**2) Health depth: L1 / L2 / L3**

| Level | Meaning (examples) |
|-------|---------------------|
| **L1** | Reachability: TCP/HTTP up; auth token accepted |
| **L2** | Functional: model gateway lists models; MCP enumerates tools; browser bridge creates session |
| **L3** | Transactional: tiny inference succeeds; safe tool noop; minimal browser metadata fetch |

Most stacks stop at L1; L2/L3 are required for “feels reliable.”

**3) Timeouts, retries, backoff**  
Per **class** of operation (model list vs chat stream vs MCP discovery vs tool exec vs browser): budgets, retry only on **network / 502 / 503 / gateway timeout**, no retry on **auth / schema / policy**, exponential backoff with jitter, caps by operation type.

### 10.5 Model Gateway reliability (OpenClaw’s highest-leverage path)

- **Provider metadata:** type (Ollama/vLLM), supported APIs (chat, responses, embeddings, streaming), concurrency, warm/cold hints, latency signals, last failure, context limits, load/unload behavior.
- **Fallback chains:** Prefer capability-based routing (e.g. primary model → fallback models/providers per policy) when a target is unavailable or overloaded.
- **Warmup / preflight:** Optional prewarm of default model; queue depth or cold-start flags in health; user-visible **degraded** mode when lazy-loading.
- **Streaming robustness:** Preserve stream semantics; clean recovery when upstream closes; annotate incomplete generations; avoid hung sockets on stall.
- **Schema normalization:** Stable error envelope, finish reasons, tool-call shapes, token accounting, embeddings format—reduce OpenClaw fragility from provider quirks.

### 10.6 MCP Gateway reliability

- **Curated tool surface to OpenClaw:** enabled tools only; healthy tools only; tools allowed for **this** client/agent; stable schemas; versioned metadata—**not** raw chaos for the model to debug.
- **Per-tool / per-server circuit breakers:** failure counts; open/half-open/closed; quarantine; auto recheck; user-visible degradation.
- **Schema validation:** On registration and periodically—reject or quarantine drift.
- **Execution classes:** fast stateless vs network-bound vs long-running vs side-effecting—distinct timeouts, confirmation policy, retries.
- **Provenance:** `request_id`, `session_id`, `agent_id`, tool name, server version, duration, result code, **failure category**—extend correlation story beyond today’s `X-Request-ID`.

**Alignment:** PRD already plans auto-disable after repeated health failures (M6); treat as **mandatory** for production-grade MCP, not optional polish.

### 10.7 Browser / bridge reliability

Typical failures: stale sessions, expired auth, headless crashes, navigation hangs, DOM mismatch vs tool expectations.

**Requirements (directional):** explicit browser **session lifecycle** APIs; health check **separate** from OpenClaw UI; idle timeouts / leases; diagnostics events; recycle crashed sessions; operator metrics (“last successful browser action”). **Browser automation is an optional module**, not a hard dependency for ordinary chat.

### 10.8 Ops controller: recovery, not hot path

**Rule:** Normal model and tool traffic flows **OpenClaw → model gateway** and **OpenClaw → MCP gateway** directly. **Dashboard** observes and administers. **Ops controller** restarts services, surfaces logs, coordinates upgrades. **No user request should require ops-controller success** to complete a chat or tool call.

### 10.9 Observability (elite bar)

- **Golden trace:** One `request_id` from OpenClaw ingress through model selection, model gateway, provider, MCP bridge, MCP gateway, tool server, optional browser bridge—dashboard can show **where time went** and **where failure occurred**.
- **Failure taxonomy:** e.g. network, auth, config, dependency unavailable, timeout, schema mismatch, policy denied, provider overload, internal bug—actionable signal, not raw log soup.
- **SLO-style reporting:** model gateway success rate, p50/p95 latency, MCP tool success rate, top failing tools, OpenClaw request success rate, degraded-session rate, restarts per service.

### 10.10 Configuration management

- Version and **validate** config on startup; deprecations and safe auto-migration where possible; risky migrations require explicit confirmation; export/import of known-good configs.
- **OpenClaw integration checks:** bridge plugin present, model gateway endpoint, tokens, default model exists, browser port assumptions, secure override compatibility.
- **Stack “doctor”** command (high value): one diagnostic entrypoint for the whole toolkit.

### 10.11 Security and reliability together

Items that are both security and reliability problems: Open WebUI auth default, MCP per-client enforcement gaps, plaintext tokens in `openclaw.json`. **Improvements:** auth on by default for remotely reachable UIs; env-based secret resolution where possible; explicit per-client policy at MCP gateway; tool registration workflow; immutable audit trail for config changes.

### 10.12 Roadmap summary (maps to M7)

| Phase | Focus | Outcome |
|-------|--------|---------|
| **1** | Visibility | Typed readiness, registry, correlation, taxonomy, dashboard dependency page, OpenClaw startup validation, E2E smoke tests |
| **2** | Degradation & recovery | Fallbacks, circuit breakers, warm/cold reporting, retries, auto-disable bad tools, ops-assisted restart, browser recycle |
| **3** | Operator-grade | SLOs, pinned bundles, rollback, `BASE_PATH` backup/restore, config migrations, release/integration matrix |

### 10.13 What not to do

- Turn AI-toolkit into a **monolithic OpenClaw fork**.
- Make the **dashboard** a **required** runtime dependency for normal inference or tools.
- Put **ops-controller** on the **hot path** for every user action.
- Add **more services** before **dependency contracts** are hardened.
- Rely on **restart** as the primary reliability strategy.

---

## Appendix A — Environment Variables Reference

| Variable | Service | Description | Default |
|----------|---------|-------------|---------|
| `BASE_PATH` | compose | Project root path | `.` |
| `DATA_PATH` | compose | Data directory | `${BASE_PATH}/data` |
| `OLLAMA_URL` | model-gateway, dashboard | Ollama internal URL | `http://ollama:11434` |
| `VLLM_URL` | model-gateway | vLLM internal URL (optional) | `` |
| `DEFAULT_PROVIDER` | model-gateway | Provider for unprefixed models | `ollama` |
| `MODEL_CACHE_TTL_SEC` | model-gateway | Model list cache TTL seconds | `60` |
| `DASHBOARD_URL` | model-gateway | Dashboard for throughput recording | `http://dashboard:8080` |
| `OPS_CONTROLLER_URL` | dashboard | Ops controller URL | `http://ops-controller:9000` |
| `OPS_CONTROLLER_TOKEN` | dashboard, ops-controller | Bearer token for ops API | *(required)* |
| `DASHBOARD_AUTH_TOKEN` | dashboard | Bearer token for dashboard API | *(optional)* |
| `DEFAULT_MODEL` | dashboard, open-webui | Default model shown in Open WebUI chat | *(optional)* |
| `OPENCLAW_GATEWAY_TOKEN` | openclaw | Gateway auth token | *(required)* |
| `OPENCLAW_GATEWAY_PORT` | openclaw-gateway | OpenClaw gateway host port | `6680` (default; secure override often **18789**) |
| `OPENCLAW_BRIDGE_PORT` | openclaw-gateway | OpenClaw bridge host port | `6681` (default; secure override often **18790**) |
| `OPENCLAW_CONFIG_DIR` | openclaw | OpenClaw config directory | `${BASE_PATH}/data/openclaw` |
| `OPENCLAW_WORKSPACE_DIR` | openclaw | OpenClaw workspace directory | `${BASE_PATH}/data/openclaw/workspace` |
| `MCP_GATEWAY_PORT` | mcp-gateway | MCP gateway host port | `8811` |
| `MODEL_GATEWAY_PORT` | model-gateway | Model gateway host port | `11435` |
| `WEBUI_AUTH` | open-webui | Enable Open WebUI auth | `False` (current); target `True` in M6 |
| `OPENAI_API_BASE` | open-webui, n8n | OpenAI-compat base URL | `http://model-gateway:11435/v1` |
| `MODELS` | model-puller | Models to pull on startup | `deepseek-r1:7b,...` |
| `COMPUTE_MODE` | compose | CPU/nvidia/amd | auto-detected |
| `QDRANT_PORT` | qdrant | Qdrant host port | `6333` |
| `EMBED_MODEL` | rag-ingestion | Embedding model for RAG | `nomic-embed-text` |
| `RAG_COLLECTION` | rag-ingestion, dashboard | Qdrant collection name | `documents` |
| `RAG_CHUNK_SIZE` | rag-ingestion | Token chunk size for document splitting | `400` |
| `RAG_CHUNK_OVERLAP` | rag-ingestion | Token overlap between chunks | `50` |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | mcp-gateway | GitHub MCP server token | *(optional)* |

---

## Appendix B — Rollback Procedures

1. **Model gateway:** Point services directly to Ollama (`OLLAMA_BASE_URL=http://ollama:11434`); `docker compose stop model-gateway`. Restart affected services.
2. **Ops controller:** Remove controller from compose or set no token; ops buttons show "unavailable" in dashboard. No data loss.
3. **MCP registry:** Delete `registry.json`; dashboard falls back to `servers.txt` only. Policy metadata disabled.
4. **cap_drop / read_only:** Remove from compose; `docker compose up -d --force-recreate <service>`.
5. **Reset OPS_CONTROLLER_TOKEN:** `openssl rand -hex 32` → update `.env` → `docker compose up -d dashboard ops-controller`.
6. **Reset OPENCLAW_GATEWAY_TOKEN:** Update `.env` → `docker compose restart openclaw-gateway` → re-pair clients.
7. **MCP tools:** Clear `data/mcp/servers.txt` or set to single safe server → gateway hot-reloads within 10s.
8. **RAG:** `docker compose stop rag-ingestion qdrant`; remove `VECTOR_DB=qdrant` from Open WebUI env → Open WebUI uses built-in vector store. Qdrant data preserved in `data/qdrant/`.
9. **Invalidate model cache:** `curl -X DELETE http://localhost:11435/v1/cache` — forces fresh fetch from Ollama on next `/v1/models` call.
10. **Safe mode:** `docker compose stop mcp-gateway openclaw-gateway comfyui rag-ingestion` → Ollama + Open WebUI + dashboard only.

---

## Appendix C — Quality Bar

**Test suite (current `tests/`):**

| File | Coverage |
|------|----------|
| `test_model_gateway_contract.py` | `/v1/models`, `/v1/chat/completions`, streaming, embeddings |
| `test_model_gateway_cache.py` | TTL cache, stale-serve, cache invalidation |
| `test_ops_controller_audit.py` | Audit schema, auth, confirm body |
| `test_dashboard_health.py` | Dashboard health endpoint, service health aggregation |
| `test_mcp_policy.py` | MCP server add/remove, registry metadata |
| `test_compose_smoke.py` | Compose config valid; optional `RUN_COMPOSE_SMOKE=1` runtime smoke |

**Missing (M6):**
- `test_responses_api.py` — Responses API format, tool conversion
- `test_rag_ingestion.py` — Document chunking, embedding, Qdrant storage
- CI workflow (`.github/workflows/test.yml`)

**Performance targets:**
- Model list (cached): `<100ms` after first call
- Model list (cold): `<2s` when Ollama healthy
- RAG embedding: `<5s` per document chunk (depends on model)
- Tool invocation: `<30s` default timeout
- Ops restart: `<60s` for most services
- Dashboard health: `<500ms`

**Security review checklist (per PR):**
- [ ] No secrets introduced in code or compose (check `git diff` for tokens)
- [ ] New services: non-root user, `cap_drop`, `security_opt`, log rotation, resource limits
- [ ] New endpoints: auth required for mutating operations
- [ ] New MCP tools: `allow_clients` explicitly set in registry
- [ ] No new host port exposures without justification
- [ ] Audit events emitted for all privileged actions
- [ ] New env vars documented in Appendix A and `.env.example`

**Break-glass:**
1. Reset admin token: see Appendix B #5
2. Restore data: `rsync -a <backup>/data/ data/`; `docker compose up -d`
3. Disable all tools: `echo "" > data/mcp/servers.txt`
4. Invalidate model cache: `curl -X DELETE http://localhost:11435/v1/cache`
5. Disable unsafe services: `docker compose stop mcp-gateway openclaw-gateway comfyui rag-ingestion`
6. Safe mode: `docker compose up -d ollama model-gateway dashboard open-webui qdrant`
