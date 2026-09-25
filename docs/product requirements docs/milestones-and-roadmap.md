# Milestones & Roadmap

## Milestone Summary

| Milestone | Status | User-visible Outcomes |
|-----------|--------|----------------------|
| **M0** | Done | Audit schema, Docker healthchecks, log rotation, SECURITY.md, runbooks |
| **M1** | Done | Model Gateway: OpenAI-compat, llama.cpp, streaming, embeddings, throughput |
| **M2** | Done | Ops Controller: start/stop/restart/logs/pull/audit; dashboard calls controller |
| **M3** | Done | MCP server registry + health API; cap_drop/read_only hardening; model list cache; Open WebUI → gateway default |
| **M4** | Done | Single `ordo-net` Docker network (edge-only host-port publish); correlation IDs (X-Request-ID forwarded dashboard → ops-controller); smoke tests |
| **M5** | Done | Dashboard MCP health dots (green/yellow/red); SSRF egress scripts; hardware stats; throughput benchmark; model switch |
| **M5-ext** | Done | RAG pipeline (Qdrant + rag-ingestion); Open WebUI → Qdrant; RAG status endpoint; Responses API + completions compat; cache-bust endpoint |
| **M6** | Partial | **Done:** MCP servers backend-only on `ordo-mcp-net`; per-consumer MCP scoping via LiteLLM virtual keys; CI; audit log rotation. **Skipped:** `WEBUI_AUTH` default → True |
| **M7** | Core done | **Done:** model-gateway `/health` + `/ready`; `ordo doctor`; CI fixture validation. **Remaining:** L3 semantics, retry/circuit policies, MCP hardening, golden traces, browser session lifecycle |

---

## M3 — MCP Health + Compose Hardening + Model Cache (Done)

**User-visible outcomes:**
- Dashboard shows green/yellow/red health badge per MCP tool
- `filesystem` no longer silently broken by default
- Model list loads faster (cached); gateway survives llama.cpp brief downtime
- Open WebUI defaults to gateway endpoint

**Acceptance criteria:**
- **Given** `searxng` enabled in `ordo.yaml`'s `plugins:`, **When** `GET /api/mcp/health`, **Then** the response contains a `searxng` entry with `status: healthy` and `tool_count > 0`, sourced from `/v1/mcp/server/health` + `tools/list` `server_outcomes`
- **Given** `ordo up --all`, **When** `docker inspect model-gateway`, **Then** `HostConfig.CapDrop` contains `ALL`, `ReadonlyRootfs` is `true`

---

## M4 — Networks + Correlation + Smoke Tests (Done)

**User-visible outcomes:**
- Single `ordo-net` Docker network (edge-only host-port publish); llama.cpp/ops-controller reachable only in-network
- Request IDs: `X-Request-ID` forwarded dashboard → ops-controller (not yet recorded in audit entries)
- Smoke tests: `scripts/smoke_test.sh` and `.ps1`

---

## M5 — Dashboard UI + SSRF + Stats (Done)

- MCP health dots (green/yellow/red) per tool
- SSRF scripts: `scripts/ssrf-egress-block.sh` and `.ps1`
- Hardware stats: the Overview page (`GET /api/overview`)
- Throughput benchmark: `POST /api/throughput/benchmark`
- Model switch: `POST /api/models/switch` (catalog id → ops-controller render → recreate `llamacpp` + `model-gateway`)

## M5-ext — RAG + APIs (Done)

- RAG pipeline: Qdrant + `rag-ingestion` + Open WebUI → Qdrant
- Responses API: `/v1/responses`
- Completions compat: `/v1/completions`
- Cache invalidation: `DELETE /v1/cache`

---

## M6 — Partial (Non-Auth Track)

### Shipped

| Item | Notes |
|------|--------|
| MCP servers → backend only | `ordo-mcp-net` (`internal: true`), reachable only by `model-gateway`; no host port published (edge-only publish model) |
| Per-consumer MCP scoping | LiteLLM virtual-key `object_permission.mcp_servers` grants with `require_key_mcp_access_defined: true` |
| CI pipeline | `.github/workflows/ci.yml` |
| Audit log rotation | ops-controller `ordo/control/audit.py`: rotates at 10 MB, keeps five generations |

### Still Open / Deferred

| Item | Rationale | Effort |
|------|-----------|--------|
| `WEBUI_AUTH` default → `True` | Security: Open WebUI ships open by default | XS |
| Per-tool `allowed_tools` narrowing | Schema supports it; no manifest sets it yet (server-level scoping already ships) | S |
| RBAC (read-only role) | View logs/health without start/stop access | L |

### M6 Acceptance Criteria

- **Given** `ordo up --all`, **When** env does not set `WEBUI_AUTH`, **Then** Open WebUI requires login
- **Given** `docker inspect ordo-mcp-searxng-1`, **Then** `NetworkSettings.Networks` contains `ordo-mcp-net` (and `ordo-net` only when the manifest declares `network: stack`)
- **Given** audit log exceeds 10MB, **When** next privileged action occurs, **Then** old log renamed to `audit.1.log` (five generations kept)
- **Given** push to main branch, **When** CI runs, **Then** all contract + smoke tests pass

---

## M7 — Reliability Spine

**Outcome:** When an agent or other client fails, operators can tell **which hop** failed and whether the failure is **retryable** or **operator-action-required**.

**Phase 1 (failures visible):** Typed `/health` and `/ready` for model gateway (including `/v1/mcp/server/health` for the MCP servers) and browser bridge; dependency registry in config + dashboard surface; `X-Request-ID` / correlation end-to-end; failure taxonomy; dashboard dependency status; agent startup validation; smoke tests.

**Phase 2 (degradation & recovery):** Provider fallback chains; per-tool / per-server circuit breakers; cold/warm model state; standardized timeout & retry budgets; auto-disable/quarantine unhealthy tools; ops-controller restart hooks; browser bridge session health / recycle.

**Phase 3 (operator-grade):** SLO dashboard; version-pinned bundles; rollback; `BASE_PATH` backup/restore; config migration engine; expanded integration test matrix.

**Explicit non-goals:** Dashboard as required runtime dependency; ops-controller in hot path; new services before contracts harden; "restart fixes it" as primary strategy.

---

## Test Plan (Current)

```bash
# Unit/contract tests
python -m pytest tests/ -v

# Compose smoke (render, then bring up from out/)
python -m ordo --source out/ordo.yaml render --out out
ordo up --all
docker ps --filter label=com.docker.compose.project=ordo   # all services healthy within 3 min
# model-gateway publishes NO host port (only Caddy publishes host ports): reach it in-network from another container:
docker exec ordo-dashboard-1 curl -s http://model-gateway:11435/v1/models | jq .data[].id
# or externally via the Caddy /llm edge route (bearer = LITELLM_MASTER_KEY):
# curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" https://<host>/llm/v1/models | jq .data[].id
docker compose exec dashboard curl -s http://localhost:8080/api/mcp/health | jq .health
docker compose exec dashboard curl -s http://localhost:8080/api/overview | jq .knowledge
docker inspect $(docker compose ps -q model-gateway) --format '{{.HostConfig.CapDrop}}'
# → [ALL]
```
