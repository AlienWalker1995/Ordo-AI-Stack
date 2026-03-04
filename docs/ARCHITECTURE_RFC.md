# AI Platform-in-a-Box — Architecture RFC

**Status:** Living document — updated 2026-03-01
**Scope:** Transform LLM-toolkit into a cohesive local-first AI platform.
**Prior draft:** 2025-02-28 (largely implemented; this doc reflects confirmed code state).

---

## SECTION 0 — Executive Summary

**What we're building:** A local-first AI platform where (1) every service reaches every model via one OpenAI-compatible gateway, (2) MCP tools are shared with a registry, health checks, and policy controls, (3) a dashboard manages full service lifecycle through a secure authenticated control plane, (4) every privileged action is audited and reviewable, and (5) Docker/Compose practices are hardened, reproducible, and observable.

**Biggest wins (already delivered):** Model Gateway with Ollama + vLLM adapters is live. Ops Controller with bearer auth, start/stop/restart/logs/pull, and append-only JSONL audit log is live. Dashboard auth middleware (Bearer + Basic) is live. OpenClaw routes through the gateway with throughput recording. Contract tests, audit schema, SECURITY.md, and runbooks exist.

**Biggest wins (next):** MCP registry.json integration + per-server health in the dashboard UI; `cap_drop` / `security_opt` / explicit networks across all compose services; model list TTL cache; correlation ID propagation from model gateway → audit log; Open WebUI defaulting to the gateway endpoint.

**Biggest risks:** `docker.sock` exposure in both `mcp-gateway` and `ops-controller` — two surfaces; MCP `filesystem` server enabled by default in `servers.txt` but broken/permissive without root-dir config; `WEBUI_AUTH=False` in Open WebUI ships open by default; runtime `openclaw.json` contains plaintext API keys and tokens (in gitignored `data/` — safe from commits, but risk if `data/` is ever shared).

---

## SECTION 1 — Current State (Grounded)

*Grounded by reading: `model-gateway/main.py`, `ops-controller/main.py`, `dashboard/app.py`, `docker-compose.yml`, `data/openclaw/openclaw.json`, `.env`, all docs, tests.*

### Architecture Summary

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Host                                                                        │
│  ┌───────────────┐  ┌───────────┐  ┌───────────────────────────────────────┐│
│  │ Open WebUI    │  │   N8N     │  │  OpenClaw Gateway  :18789/:18790       ││
│  │ :3000         │  │ :5678     │  │  (model-gateway + mcp-bridge plugin)   ││
│  └───────┬───────┘  └─────┬─────┘  └───────────────────────────────────────┘│
│          │                │                      │                           │
│  OLLAMA_BASE_URL       MCP client           gateway provider                 │
│  (still direct)        (int)            + openclaw-mcp-bridge                │
│  ─────────────────────────┼──────────────────────┼───────────────────────── │
│  Docker network: ai-toolkit_default (auto)        │                          │
│  ┌─────────────────┐  ┌──────────────┐  ┌─────────────────┐  ┌────────────┐ │
│  │ Model Gateway   │  │ MCP Gateway  │  │ Ops Controller  │  │ Dashboard  │ │
│  │ :11435          │  │ :8811        │  │ :9000 (int)     │  │ :8080      │ │
│  │ /v1/models      │  │ docker.sock  │  │ docker.sock     │  │ no sock    │ │
│  │ /v1/chat/...    │  │ servers.txt  │  │ bearer auth     │  │ bearer/pw  │ │
│  │ /v1/embeddings  │  │ (no registry)│  │ audit log       │  │ → ctrl API │ │
│  └────────┬────────┘  └──────────────┘  └─────────────────┘  └────────────┘ │
│           │                                                                  │
│  ┌────────▼────────┐  ┌──────────────┐  ┌──────────────┐                    │
│  │ Ollama :11434   │  │ ComfyUI      │  │ vLLM (future)│                    │
│  │ (native API)    │  │ :8188        │  │ (VLLM_URL)   │                    │
│  └─────────────────┘  └──────────────┘  └──────────────┘                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

### What Already Satisfies G1–G5 (Confirmed by Code)

| Goal | Current Support | Code Evidence |
|------|-----------------|---------------|
| **G1: Any service → any model** | Good. Gateway at `:11435` with Ollama + vLLM adapters; streaming, embeddings, tool-calling pass-through. OpenClaw routes via gateway. Open WebUI still defaults to direct Ollama. | `model-gateway/main.py` |
| **G2: Shared tools with policy** | Partial. MCP Gateway shares tools via `servers.txt` (10s hot-reload). `registry.json` NOT read. No policy, no health, no rate limits. | `docker-compose.yml`, `data/mcp/servers.txt` |
| **G3: Dashboard as control center** | Good. Ops Controller: start/stop/restart/logs/pull; no host port; bearer auth. Dashboard calls controller. Start/Stop buttons present per code. | `ops-controller/main.py` |
| **G4: Security + auditing** | Good. Audit JSONL with `ts/action/resource/actor/result/detail/correlation_id`. Bearer auth on controller. Dashboard auth middleware (Bearer + Basic). `SECURITY.md` + threat table. | `ops-controller/main.py`, `dashboard/app.py` |
| **G5: Docker best practices** | Partial. Healthchecks ✓, resource limits ✓, log rotation ✓ (4 services), non-root ✓ (model-gateway, dashboard). `cap_drop`, `security_opt`, `read_only`, explicit networks missing. n8n/comfyui no log rotation. | `docker-compose.yml` |

### Pain Points / Gaps (Mapped to G1–G5)

| Gap | Goal | Description | Severity |
|-----|------|-------------|----------|
| Open WebUI → direct Ollama | G1 | `OLLAMA_BASE_URL=http://ollama:11434` default bypasses gateway; throughput not recorded; future providers missed | Medium |
| MCP: registry.json unused | G2 | Gateway reads only `servers.txt`; registry.json example exists but wrapper ignores it | High |
| MCP: no per-server health | G2 | Failing tools stay enabled; no dashboard health badges | High |
| MCP: filesystem enabled broken | G2 | `duckduckgo,hugging-face,filesystem` in servers.txt; filesystem fails without root-dir config | Medium |
| No `cap_drop` / `security_opt` | G5 | No `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]` on any service | High |
| No explicit Docker networks | G5 | Using auto-created `ai-toolkit_default`; no named networks declared; no isolation between trust tiers | Medium |
| Services running as root | G5 | `mcp-gateway`, `ops-controller`, `n8n`, `comfyui`, `openclaw-gateway`, `model-puller` run as root | High |
| No log rotation: n8n, comfyui | G5 | Model-gateway/dashboard/ops-controller/mcp-gateway have log rotation; n8n and comfyui do not | Low |
| Model list not cached | G1 | Every `/v1/models` call hits Ollama live; if Ollama is slow, gateway is slow | Medium |
| No correlation ID from gateway | G4 | Model gateway does not emit `X-Request-ID`; audit entries cannot be correlated with model calls | Medium |
| `WEBUI_AUTH=False` default | G4 | Open WebUI ships open; documented but not enforced | Low |
| openclaw-gateway: no limits | G5 | No `deploy.resources.limits` for openclaw-gateway | Low |
| Runtime secrets in openclaw.json | G4 | Telegram token, skill API key in `data/openclaw/openclaw.json` plaintext; gitignored but present on disk | Medium |

### OpenClaw: Current Integration Map (Confirmed)

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

---

## SECTION 3 — Target Architecture

### Components

- **Model Gateway** `:11435` — OpenAI-compatible proxy; provider abstraction (Ollama, vLLM); model registry with TTL cache; throughput recording; `X-Request-ID` propagation.
- **MCP Gateway** `:8811` — Docker MCP Gateway with hot-reload; enhanced with `registry.json` metadata reader; per-server health; docker.sock for spawning server containers.
- **Ops Controller** `:9000` (internal) — Authenticated REST; start/stop/restart/logs/pull; append-only JSONL audit log; docker.sock access with allowlisted operations only.
- **Dashboard** `:8080` — No docker.sock; calls controller for ops; model inventory; MCP tool management + health badges; throughput stats. Auth: Bearer token or Basic password.
- **Ollama** `:11434` — LLM inference; GPU optional via `docker-compose.compute.yml` (auto-generated).
- **OpenClaw Gateway** `:18789/:18790` — Agentic runtime; routes models via gateway provider; MCP tools via bridge plugin.
- **Supporting services** — Open WebUI (`:3000`), N8N (`:5678`), ComfyUI (`:8188`), openclaw sync/config/plugin services.

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
│  │ Open WebUI  │ │   N8N    │ │  OpenClaw Gateway  :18789                │   │
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
| `/v1/chat/completions` | POST | Chat; routes by model prefix (`ollama/`, `vllm/`); streaming ✓ |
| `/v1/embeddings` | POST | Embeddings; Ollama `/api/embed` + vLLM pass-through |
| `/health` | GET | Gateway health; checks at least one provider reachable |

**Model naming:**
- `ollama/deepseek-r1:7b` → Ollama
- `vllm/llama3` → vLLM (if `VLLM_URL` set)
- `deepseek-r1:7b` (no prefix) → `DEFAULT_PROVIDER`

**Headers:** `X-Service-Name: <caller>` (for throughput attribution); `X-Request-ID: <uuid>` (for correlation).

**Config:**
```yaml
# docker-compose.yml
model-gateway:
  environment:
    - OLLAMA_URL=http://ollama:11434
    - VLLM_URL=${VLLM_URL:-}
    - DEFAULT_PROVIDER=ollama
    - DASHBOARD_URL=http://dashboard:8080
    - MODEL_CACHE_TTL_SEC=60     # NEW: add this
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

**Policy API** (dashboard `/api/mcp`):
- `GET /api/mcp/servers` — enabled list with registry metadata
- `POST /api/mcp/servers` — add tool (updates `servers.txt` + registry)
- `DELETE /api/mcp/servers/{name}` — remove tool
- `GET /api/mcp/health` — per-server health status: `{server: {ok: bool, last_checked: ts}}`

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

---

## SECTION 4 — Workstreams (Detailed)

### WS1: Unified Model Access

**Status: M1 ✅ Done. Remaining: caching, Open WebUI default, vLLM compose profile.**

**Provider abstraction (implemented in `model-gateway/main.py`):**
- `_model_provider_and_id(name)` → `(provider, model_id)` by prefix
- Ollama: translate to `/api/chat`, `/api/embed`; delta streaming
- vLLM: native OpenAI format; proxy directly

**Missing: TTL model list cache.** Add to `model-gateway/main.py`:
```python
import time
_model_cache: list = []
_model_cache_ts: float = 0.0
MODEL_CACHE_TTL = float(os.environ.get("MODEL_CACHE_TTL_SEC", "60"))

@app.get("/v1/models")
async def list_models():
    global _model_cache, _model_cache_ts
    if time.monotonic() - _model_cache_ts < MODEL_CACHE_TTL and _model_cache:
        return {"object": "list", "data": _model_cache}
    # ... fetch from Ollama/vLLM ...
    _model_cache = objects
    _model_cache_ts = time.monotonic()
    return {"object": "list", "data": objects}
```

**Missing: `X-Request-ID` propagation.** Add to `model-gateway/main.py`:
```python
import uuid
@app.post("/v1/chat/completions")
async def chat_completions(request: Request, body: dict[str, Any]):
    req_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:12]}"
    # pass req_id to _record_throughput and include in response headers
```

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

**Status: Gateway working with servers.txt. Registry.json not implemented. Health not implemented.**

**Registry integration plan:**

The Docker MCP Gateway (`mcp-gateway`) reads `servers.txt` for enabled tools. The gateway wrapper script (`mcp/gateway-wrapper.sh` or equivalent) controls this. The `registry.json` is a metadata layer we own; it does NOT replace `servers.txt` but decorates it.

**Implementation:** Dashboard reads both `servers.txt` (enabled list) and `registry.json` (metadata) to produce enriched tool view. When adding/removing tools, dashboard updates `servers.txt`; registry.json is the source of truth for metadata.

**Dashboard health endpoint** (`dashboard/app.py`):
```python
@app.get("/api/mcp/health")
async def mcp_health():
    """Probe each enabled MCP server by calling the MCP gateway's tool list."""
    enabled = _read_servers_txt()  # parse data/mcp/servers.txt
    results = {}
    async with AsyncClient(timeout=5.0) as client:
        for server in enabled:
            try:
                r = await client.get(f"{MCP_GATEWAY_URL}/mcp", timeout=5.0)
                results[server] = {"ok": r.status_code < 500, "checked_at": _now_iso()}
            except Exception as e:
                results[server] = {"ok": False, "error": str(e), "checked_at": _now_iso()}
    return {"health": results}
```

**Policy (initial):**
- `allow_clients: ["*"]` = all clients get the tool (default for new tools)
- `allow_clients: []` = tool disabled (use for `filesystem` until configured)
- Per-client enforcement: future M3 (requires client identity header `X-Client-ID`)

**Secrets strategy:**
- Add `mcp/.env` with `GITHUB_PERSONAL_ACCESS_TOKEN=`, `BRAVE_API_KEY=`, etc.
- Document in registry `env_schema`
- Dashboard: "Configure secrets" → links to docs page with instructions
- No secrets input in dashboard UI (avoid storing keys in dashboard config)

**Health lifecycle:**
- Dashboard polls `/api/mcp/health` every 60s
- Dashboard UI shows green/yellow/red per tool
- Future: auto-remove from `servers.txt` after 3 consecutive failures (with alert)

**Fix: filesystem default:**
```
# data/mcp/servers.txt — change from:
duckduckgo,hugging-face,filesystem
# to:
duckduckgo,hugging-face
```
Remove `filesystem` from default; add to `registry.json` with `allow_clients: []`. Document how to enable.

**OpenClaw-specific:**
- OpenClaw uses `openclaw-mcp-bridge` plugin → `http://mcp-gateway:8811/mcp` ✓ (already working)
- Tools surface as `gateway__duckduckgo_search`, etc.
- No config change needed for existing setup
- Future per-agent policy: add `X-Client-ID: openclaw` header to bridge plugin config; gateway checks allowlist

### WS3: Dashboard as Control Center (Ops)

**Status: M3 ✅ Done. Controller has start/stop/restart/logs/pull/audit. Dashboard calls controller. No docker.sock in dashboard.**

**Confirmed implementation:**
- `ops-controller/main.py`: `verify_token` Depends; `ALLOWED_SERVICES` allowlist; `ConfirmBody(confirm, dry_run)` for all mutating ops; `_audit()` writes JSONL
- `dashboard/app.py`: auth middleware on `/api/*` (except `/api/health`, `/api/auth/config`); calls `OPS_CONTROLLER_URL` with token

**Remaining gaps:**
1. `actor` field in `_audit()` is hardcoded to `"dashboard"`. Should accept from request context or be configurable.
2. No CSRF token — sufficient for localhost; acceptable for now.
3. Audit entries from `logs` action don't include `tail` count in `metadata`. Add it.

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
| Dashboard auth | Unauthenticated admin | Optional (`DASHBOARD_AUTH_TOKEN` / `DASHBOARD_PASSWORD`) | Document: set one of these; pre-deployment checklist item |
| `openclaw.json` plaintext keys | Key exposure if file shared/backed up | In gitignored `data/`; acceptable on local disk | Flag in docs: avoid including `data/openclaw/` in cloud backups without encryption |
| WEBUI_AUTH=False | Open WebUI accessible without auth | Explicit in compose env | Change default to `WEBUI_AUTH=${WEBUI_AUTH:-True}`; opt-out, not opt-in |
| Model gateway | No auth on `/v1/` endpoints | None; local-first intentional | Acceptable for localhost; add API key support if exposed to LAN |

**AuthN/AuthZ approach:**
- **Tier 0:** No auth (health endpoints, read-only model list)
- **Tier 1:** Bearer token (ops controller — `OPS_CONTROLLER_TOKEN`; optional dashboard — `DASHBOARD_AUTH_TOKEN`)
- **Tier 2:** Password (dashboard — `DASHBOARD_PASSWORD` for Basic auth via browser)
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
Document in `docs/runbooks/SECURITY_HARDENING.md` (new file).

### WS5: Best-in-Class Docker/Compose & Repo Organization

**Compose hardening checklist — applied to current `docker-compose.yml`:**

| Check | Current | Action |
|-------|---------|--------|
| Non-root | `model-gateway`, `dashboard`: `user: "1000:1000"` ✓. `mcp-gateway`, `ops-controller`, `n8n`, `comfyui`, `openclaw-gateway`: root | Add `user: "1000:1000"` where feasible; ops-controller needs docker group → `user: "1000:999"` (check docker GID) |
| `cap_drop: [ALL]` | Not set on any service | Add to all custom services (`model-gateway`, `dashboard`, `ops-controller`). N8N, comfyui: add after testing. |
| `security_opt: [no-new-privileges:true]` | Not set | Add to all custom-build services |
| `read_only: true` | Not set | Add to `model-gateway`, `dashboard` with tmpfs for `/tmp` |
| Healthchecks | Present on ollama, model-gateway, dashboard, mcp-gateway, comfyui, n8n, open-webui ✓ | Add to openclaw-gateway (probe `:18789`) |
| Resource limits | Most services have memory limits ✓. `openclaw-gateway`, `n8n`: check | Add `deploy.resources.limits.memory: 1G` to openclaw-gateway; verify n8n has limits |
| Log rotation | `model-gateway`, `dashboard`, `ops-controller`, `mcp-gateway` ✓. `n8n`, `comfyui`: missing | Add `logging:` block to n8n and comfyui |
| Pinned images | `ollama:0.17.4` ✓, `open-webui:v0.8.4` ✓, `curlimages/curl:8.10.1` ✓, `python:3.12.8-slim` ✓ | Add digest comments for critical services: see digest comment in compose already for ollama |
| Explicit networks | Not declared; using auto `ai-toolkit_default` | Declare named networks; separate `frontend` (open to host) from `backend` (internal) |
| Named volumes | Not declared; using bind mounts | Bind mounts are acceptable for local-first; document backup story |
| `restart: unless-stopped` | Present on all long-running services ✓ | No change |
| One-shot services | `restart: "no"` ✓ on pullers, sync services | No change |

**Compose specific diffs (priority):**

```yaml
# Add to model-gateway:
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    # user: "1000:1000" already present ✓

# Add to dashboard:
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    # user: "1000:1000" already present ✓

# Add to ops-controller (needs docker socket group):
    user: "1000:999"   # 999 = typical docker GID; verify with: stat -c %g /var/run/docker.sock
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]

# Add to mcp-gateway:
    user: "1000:999"   # needs docker group
    security_opt: [no-new-privileges:true]

# Add log rotation to n8n and comfyui:
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

# Add resource limits to openclaw-gateway:
    deploy:
      resources:
        limits:
          memory: 2G

# Add healthcheck to openclaw-gateway:
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://localhost:18789/api/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 30s

# Change Open WebUI to default to gateway:
    environment:
      - OLLAMA_BASE_URL=${OLLAMA_BASE_URL:-http://ollama:11434}  # keep for backward compat
      - OPENAI_API_BASE=${OPENAI_API_BASE:-http://model-gateway:11435/v1}
      - WEBUI_AUTH=${WEBUI_AUTH:-True}   # safe by default

# Declare explicit networks:
networks:
  frontend:
    name: ai-toolkit-frontend
  backend:
    name: ai-toolkit-backend
    internal: true    # no direct internet access

# Fix filesystem default:
# data/mcp/servers.txt: remove filesystem (or document it must be configured)
```

**Explicit network assignment (target):**

| Service | Frontend | Backend |
|---------|----------|---------|
| open-webui | ✓ (host port) | ✓ (needs model-gateway) |
| dashboard | ✓ (host port) | ✓ (needs ollama, ops-controller, mcp-gateway) |
| n8n | ✓ (host port) | ✓ (needs mcp-gateway, model-gateway) |
| openclaw-gateway | ✓ (host port) | ✓ (needs ollama, model-gateway, mcp-gateway) |
| model-gateway | ✓ (host port for external) | ✓ (needs ollama) |
| mcp-gateway | — | ✓ (internal; no host port needed for compose services) |
| ops-controller | — | ✓ (internal only) |
| ollama | — | ✓ (internal only after switch) |
| comfyui | ✓ (host port) | — |

**Note on Ollama host port:** Ollama currently exposes `:11434` to host. Once all services use the model gateway, this can become internal-only. Keep for development/Cursor access.

**Repo structure (current state is already good; proposed additions):**
```
LLM-toolkit/
├── dashboard/           ✓ exists
├── model-gateway/       ✓ exists
├── ops-controller/      ✓ exists
├── mcp/                 ✓ exists
│   └── README.md        ✓ exists
├── openclaw/            ✓ exists
├── scripts/             ✓ exists
├── tests/               ✓ exists (contract + health tests)
│   └── test_compose_smoke.py  # NEW — see M2
├── docs/
│   ├── ARCHITECTURE_RFC.md    ✓ this file
│   ├── audit/SCHEMA.md        ✓
│   └── runbooks/
│       ├── TROUBLESHOOTING.md ✓
│       ├── BACKUP_RESTORE.md  ✓
│       ├── UPGRADE.md         ✓
│       └── SECURITY_HARDENING.md  # NEW — SSRF rules, iptables
├── data/                # gitignored, runtime data
│   ├── mcp/
│   │   ├── servers.txt  ✓
│   │   └── registry.json  # NEW — metadata
│   └── ops-controller/
│       └── audit.log    # runtime
├── docker-compose.yml   ✓
├── docker-compose.compute.yml  # auto-generated by detect_hardware.py; gitignored
├── overrides/           # optional compose overrides
│   ├── openclaw-secure.yml   # bind OpenClaw to localhost only
│   ├── ollama-expose.yml     # expose Ollama host port (Cursor, CLI)
│   └── vllm.yml              # vLLM provider profile
├── .env.example         # ensure current
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
| **M5** | 🔶 Partial | Dashboard: MCP health dots (green/yellow/red) + SSRF script; MCP policy tests when gateway supports allowlist |

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

### M5 — Next (Partial ✅)

- **Dashboard UI:** ✅ MCP health dots (green/yellow/red) per tool; gateway badge "gateway ok" / "gateway unreachable"; degraded state (yellow) for non-running container status.
- **SSRF script:** ✅ `scripts/ssrf-egress-block.sh` (Linux/WSL2) — auto-detect subnet, `--dry-run` / `--remove`; `scripts/ssrf-egress-block.ps1` (Windows) — guidance only. Runbook updated to reference scripts.
- **Policy tests:** pytest for MCP allowlist behavior when registry `allow_clients` is enforced (deferred until gateway supports it).

---

## SECTION 6 — "First PR" (Do Now)

The basic M0 items are already done (audit schema, healthchecks, log rotation, SECURITY.md). The highest-value, lowest-risk "first PR" that delivers immediate value is:

**PR: MCP health dashboard + fix filesystem default + cap_drop hardening**

This PR:
1. Fixes the broken `filesystem` MCP server default (immediate operational improvement)
2. Adds `cap_drop` + `security_opt` + `read_only` to custom services (immediate security improvement)
3. Adds MCP health endpoint to dashboard (visible operational improvement)
4. Adds model list caching (performance improvement)
5. Changes `WEBUI_AUTH` to default `True` (security improvement)

None of these break existing functionality.

### Exact Steps

**Step 1: Fix filesystem and create registry.json**

Create `data/mcp/registry.json`:
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
      "timeout_sec": 30
    },
    "hugging-face": {
      "image": "mcp/hugging-face",
      "description": "Hugging Face models and datasets",
      "scopes": ["search"],
      "allow_clients": ["*"],
      "timeout_sec": 30
    },
    "filesystem": {
      "image": "mcp/filesystem",
      "description": "File access. Requires FILESYSTEM_ROOT configured before enabling.",
      "scopes": ["filesystem"],
      "allow_clients": [],
      "env_schema": {
        "FILESYSTEM_ROOT": {"required": true, "secret": false}
      }
    },
    "github-official": {
      "image": "mcp/github-official",
      "description": "GitHub issues, PRs, repos. Requires GITHUB_PERSONAL_ACCESS_TOKEN.",
      "scopes": ["github"],
      "allow_clients": ["*"],
      "env_schema": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": {"required": true, "secret": true}
      }
    },
    "brave": {
      "image": "mcp/brave",
      "description": "Brave Search. Requires BRAVE_API_KEY.",
      "scopes": ["search"],
      "allow_clients": ["*"],
      "env_schema": {
        "BRAVE_API_KEY": {"required": true, "secret": true}
      }
    },
    "fetch": {
      "image": "mcp/fetch",
      "description": "Fetch and parse web pages",
      "scopes": ["fetch"],
      "allow_clients": ["*"],
      "timeout_sec": 30
    }
  }
}
```

Edit `data/mcp/servers.txt`:
```
duckduckgo,hugging-face
```
(remove `filesystem`)

**Step 2: Docker hardening in docker-compose.yml**

Add to `model-gateway`:
```yaml
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
```

Add to `dashboard`:
```yaml
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
```

Add to `ops-controller`:
```yaml
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
```

Change `open-webui` env:
```yaml
      - WEBUI_AUTH=${WEBUI_AUTH:-True}
```

Add to `n8n`:
```yaml
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

Add to `comfyui`:
```yaml
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

Add to `openclaw-gateway`:
```yaml
    deploy:
      resources:
        limits:
          memory: 2G
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://localhost:18789"]
      start_period: 60s
      interval: 30s
      timeout: 10s
      retries: 3
```

**Step 3: Model gateway caching**

In `model-gateway/main.py`, add before `list_models()`:
```python
import time, uuid

_model_cache: list = []
_model_cache_ts: float = 0.0
MODEL_CACHE_TTL = float(os.environ.get("MODEL_CACHE_TTL_SEC", "60"))
```

Wrap the `list_models()` fetch in a cache check; update after successful fetch.

**Step 4: MCP health endpoint in dashboard**

Add `GET /api/mcp/health` to `dashboard/app.py` that probes the MCP gateway and returns per-server health status. Read `registry.json` for known server metadata.

**Step 5: WEBUI_AUTH and OPENAI_API_BASE for Open WebUI**

In `docker-compose.yml` `open-webui.environment`:
```yaml
      - OPENAI_API_BASE=${OPENAI_API_BASE:-http://model-gateway:11435/v1}
      - WEBUI_AUTH=${WEBUI_AUTH:-True}
```

**Step 6: Add tests**

Add `tests/test_dashboard_mcp_health.py`:
```python
"""Contract test for /api/mcp/health endpoint."""
def test_mcp_health_returns_server_status(mock_mcp_gateway, client):
    """GET /api/mcp/health returns health status per enabled server."""
    r = client.get("/api/mcp/health")
    assert r.status_code == 200
    data = r.json()
    assert "health" in data
    assert isinstance(data["health"], dict)
```

Add `tests/test_model_gateway_cache.py`:
```python
"""Contract test for model gateway TTL cache."""
def test_models_cached_after_first_call(mock_ollama_tags, client):
    """Second GET /v1/models within TTL uses cached data."""
    r1 = client.get("/v1/models")
    assert r1.status_code == 200
    # Simulate Ollama going down
    with patch("httpx.AsyncClient.get", side_effect=Exception("Ollama down")):
        r2 = client.get("/v1/models")
        assert r2.status_code == 200
        assert r2.json() == r1.json()  # served from cache
```

### Suggested Commit Outline

```
commit 1: fix(mcp): remove filesystem from default servers.txt + add registry.json
  - data/mcp/servers.txt: remove filesystem
  - data/mcp/registry.json: create with full server catalog

commit 2: security(compose): add cap_drop + read_only to custom services
  - docker-compose.yml: cap_drop, security_opt, read_only for model-gateway, dashboard, ops-controller
  - docker-compose.yml: WEBUI_AUTH defaults to True

commit 3: feat(compose): add log rotation, resource limits, healthcheck to remaining services
  - docker-compose.yml: log rotation for n8n, comfyui
  - docker-compose.yml: resource limits + healthcheck for openclaw-gateway
  - docker-compose.yml: OPENAI_API_BASE for open-webui

commit 4: feat(model-gateway): add TTL model list cache
  - model-gateway/main.py: TTL cache for /v1/models
  - docker-compose.yml: MODEL_CACHE_TTL_SEC=60 env

commit 5: feat(dashboard): add MCP health endpoint + registry.json support
  - dashboard/app.py: GET /api/mcp/health
  - dashboard/app.py: enrich /api/mcp/servers with registry metadata

commit 6: test: add contract tests for mcp health + model cache
  - tests/test_dashboard_mcp_health.py
  - tests/test_model_gateway_cache.py
```

### Acceptance Criteria

- **Given** `docker compose up -d`, **When** `docker inspect ai-toolkit-model-gateway-1`, **Then** `HostConfig.CapDrop = ["ALL"]` and `HostConfig.ReadonlyRootfs = true`
- **Given** `docker compose up -d`, **When** `docker compose ps`, **Then** all running services show `healthy` within 3 minutes
- **Given** `duckduckgo` in `servers.txt` and `data/mcp/registry.json` present, **When** `GET /api/mcp/health`, **Then** `200 OK` with `{"health": {"duckduckgo": {"ok": bool, "checked_at": "..."}}}`
- **Given** Ollama is slow, **When** two `GET /v1/models` within 60s, **Then** second returns in `<100ms` (from cache)
- **Given** Open WebUI starts, **When** env does not set `WEBUI_AUTH`, **Then** WebUI requires authentication
- **Given** `filesystem` removed from `servers.txt`, **When** MCP gateway starts, **Then** no `ENOENT stat ''` errors in logs

### Test plan

```bash
# All tests pass
cd f:/LLM-toolkit && python -m pytest tests/ -v

# Compose smoke test
docker compose up -d
docker compose ps  # all healthy
curl -s http://localhost:11435/v1/models | jq .data[].id
curl -s http://localhost:8080/api/mcp/health | jq .health
docker inspect $(docker compose ps -q model-gateway) --format '{{.HostConfig.CapDrop}}'
# → [ALL]

# Open WebUI auth check
curl -s http://localhost:3000/  # should redirect to login
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

1. **Ops-controller docker GID:** What is the GID of `/var/run/docker.sock` on the host? This determines the `user: "1000:<gid>"` value for ops-controller and mcp-gateway. (`stat -c %g /var/run/docker.sock` on Linux; `999` or `0` typical.)

2. **Open WebUI OPENAI_API_BASE:** Does the current `open-webui:v0.8.4` support `OPENAI_API_BASE` env for chat + model listing? If not, does it need `OLLAMA_BASE_URL` pointed at the gateway? (Ollama-compat mode in model gateway may be needed.)

3. **MCP gateway policy:** Does `docker/mcp-gateway` support per-request client identity (e.g. `X-Client-ID` header) for allowlist enforcement? If not, per-client policy requires a sidecar proxy or upgrade.

4. **openclaw.json token externalization:** Can `merge_gateway_config.py` inject the Telegram bot token and skill API key from env vars instead of requiring them in the JSON file? This would allow sensitive values to stay in `.env`.

5. **Ollama host port:** Once all compose services use the model gateway, should Ollama's host port (`:11434`) be removed to reduce attack surface? Cursor/external dev tools currently use it directly.

6. **Audit log rotation:** Who rotates `data/ops-controller/audit.log`? Currently no rotation implemented in ops-controller (size grows unbounded). Add logrotate config or in-process rotation at 10MB?

7. **vLLM timing:** When is vLLM needed? After M3 is stable. Include `overrides/vllm.yml` as reference but don't enable by default.

8. **ComfyUI non-root:** ComfyUI runs as root (`yanwk/comfyui-boot:cpu` image). Can it run as UID 1000? Check image docs; may need `user:` override or different image.

9. **Smoke test in CI:** Is there a CI pipeline (GitHub Actions)? If yes, add `docker compose up -d && pytest tests/test_compose_smoke.py` step.

10. **N8N LLM node:** Which N8N node should be documented for model gateway access? OpenAI-compatible node with `baseURL: http://model-gateway:11435/v1`? Document with example workflow JSON.

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
  test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://localhost:18789"]
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
| `DASHBOARD_PASSWORD` | dashboard | Basic auth password for dashboard | *(optional)* |
| `OPENCLAW_GATEWAY_TOKEN` | openclaw | Gateway auth token | *(required)* |
| `MCP_GATEWAY_PORT` | mcp-gateway | MCP gateway host port | `8811` |
| `MODEL_GATEWAY_PORT` | model-gateway | Model gateway host port | `11435` |
| `WEBUI_AUTH` | open-webui | Enable Open WebUI auth | `True` (target) |
| `OPENAI_API_BASE` | open-webui, n8n | OpenAI-compat base URL | `http://model-gateway:11435/v1` (target) |
| `MODELS` | model-puller | Models to pull on startup | `deepseek-r1:7b,...` |
| `COMPUTE_MODE` | compose | CPU/nvidia/amd | auto-detected |

---

## Appendix B — Rollback Procedures

1. **Model gateway:** `OLLAMA_BASE_URL=http://ollama:11434` in service env; stop model-gateway. Restart affected services.
2. **Ops controller:** Remove controller from compose or set no token; ops buttons show "unavailable" in dashboard. No data loss.
3. **MCP registry:** Delete `registry.json`; dashboard falls back to servers.txt only. Policy metadata disabled.
4. **cap_drop / read_only:** Remove from compose; `docker compose up -d --force-recreate <service>`.
5. **Reset OPS_CONTROLLER_TOKEN:** `openssl rand -hex 32` → update `.env` → `docker compose up -d dashboard ops-controller`.
6. **Reset OPENCLAW_GATEWAY_TOKEN:** Update `.env` → `docker compose restart openclaw-gateway` → re-pair clients.
7. **MCP tools:** Clear `data/mcp/servers.txt` or set to single safe server → gateway hot-reloads within 10s.
8. **Safe mode:** `docker compose stop mcp-gateway openclaw-gateway` → use ollama + open-webui only.

---

## Appendix C — Quality Bar

**Tests:**
- Contract tests: model gateway (`/v1/models`, `/v1/chat/completions`), ops controller (audit, auth), dashboard (health, MCP health)
- Smoke test: `docker compose up -d` → all services healthy within 3 minutes
- Policy tests (M4): MCP allowlist enforcement, rate limit behavior

**Performance targets:**
- Model list (cached): `<100ms` after first call
- Model list (cold): `<2s` when Ollama healthy
- Tool invocation: `<30s` default timeout
- Ops restart: `<60s` for most services
- Dashboard health: `<500ms`

**Security review checklist (per PR):**
- [ ] No secrets introduced in code or compose (check `git diff` for tokens)
- [ ] New services: non-root user, cap_drop, security_opt
- [ ] New endpoints: auth required for mutating operations
- [ ] New MCP tools: `allow_clients` explicitly set
- [ ] No new host port exposures without justification
- [ ] Audit events emitted for all privileged actions

**Break-glass:**
1. Reset admin token: see Appendix B #5
2. Restore data: `rsync -a <backup>/data/ data/`; `docker compose up -d`
3. Disable all tools: `echo "" > data/mcp/servers.txt`
4. Disable unsafe services: `docker compose stop mcp-gateway openclaw-gateway comfyui`
5. Safe mode: Ollama + Open WebUI only
