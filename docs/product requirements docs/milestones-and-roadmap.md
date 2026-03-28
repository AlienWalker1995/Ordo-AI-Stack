# Milestones & Roadmap

## Milestone Summary

| Milestone | Status | User-visible Outcomes |
|-----------|--------|----------------------|
| **M0** | Done | Audit schema, Docker healthchecks, log rotation, SECURITY.md, runbooks |
| **M1** | Done | Model Gateway: OpenAI-compat, Ollama+vLLM, streaming, embeddings, throughput |
| **M2** | Done | Ops Controller: start/stop/restart/logs/pull/audit; dashboard calls controller; bearer auth |
| **M3** | Done | MCP registry.json + health API; cap_drop/read_only hardening; model list cache; Open WebUI → gateway default |
| **M4** | Done | Explicit Docker networks (frontend/backend); correlation IDs (X-Request-ID → audit); vLLM compose profile; smoke tests |
| **M5** | Done | Dashboard MCP health dots (green/yellow/red); SSRF egress scripts; hardware stats; throughput benchmark; default-model management |
| **M5-ext** | Done | RAG pipeline (Qdrant + rag-ingestion); Open WebUI → Qdrant; RAG status endpoint; Responses API + completions compat; cache-bust endpoint; openclaw-cli profile |
| **M6** | Partial | **Done:** mcp-gateway backend-only; CI; audit log rotation; channel token externalization. **Deferred:** MCP per-client / `X-Client-ID` (upstream). **Skipped:** `WEBUI_AUTH` default → True. **Open:** skill/plugin API keys |
| **M7** | Core done | **Done:** dependency registry + `GET /api/dependencies`; model-gateway `/health` + `/ready`; dashboard probes UI; `doctor` / `validate_openclaw_config.py`; CI fixture validation. **Remaining:** L3 semantics, retry/circuit policies, MCP hardening, golden traces, browser session lifecycle |

---

## M3 — MCP Health + Compose Hardening + Model Cache (Done)

**User-visible outcomes:**
- Dashboard shows green/yellow/red health badge per MCP tool
- `filesystem` no longer silently broken by default
- Model list loads faster (cached); gateway survives Ollama brief downtime
- Open WebUI defaults to gateway endpoint

**Acceptance criteria:**
- **Given** `duckduckgo` in `servers.txt`, **When** `GET /api/mcp/health`, **Then** response contains `{"health": {"duckduckgo": {"ok": bool, "checked_at": "..."}}}`
- **Given** `docker compose up -d`, **When** `docker inspect model-gateway`, **Then** `HostConfig.CapDrop` contains `ALL`, `ReadonlyRootfs` is `true`

---

## M4 — Networks + Correlation + vLLM + Smoke Tests (Done)

**User-visible outcomes:**
- Explicit `ordo-ai-stack-frontend` / `ordo-ai-stack-backend` networks; Ollama/ops-controller on backend only
- Request IDs: `X-Request-ID` forwarded dashboard → ops-controller and stored in audit entries
- vLLM: `overrides/vllm.yml` with profile `vllm`
- Smoke tests: `tests/test_compose_smoke.py`

---

## M5 — Dashboard UI + SSRF + Stats (Done)

- MCP health dots (green/yellow/red) per tool
- SSRF scripts: `scripts/ssrf-egress-block.sh` and `.ps1`
- Hardware stats: `GET /api/hardware`
- Throughput benchmark: `POST /api/throughput/benchmark`
- Default model management: `GET/POST /api/config/default-model`

## M5-ext — RAG + APIs (Done)

- RAG pipeline: Qdrant + `rag-ingestion` + Open WebUI → Qdrant
- Responses API: `/v1/responses`
- Completions compat: `/v1/completions`
- Cache invalidation: `DELETE /v1/cache`
- OpenClaw CLI profile

---

## M6 — Partial (Non-Auth Track)

### Shipped

| Item | Notes |
|------|--------|
| mcp-gateway → backend only | Default compose; `overrides/mcp-expose.yml` for host access |
| CI pipeline | `.github/workflows/ci.yml` |
| Audit log rotation | `ops-controller`: `AUDIT_LOG_MAX_BYTES` (default 10MB) |
| Channel token externalization | `merge_gateway_config.py`: `OPENCLAW_GATEWAY_TOKEN`; Discord/Telegram via SecretRef |

### Still Open / Deferred

| Item | Rationale | Effort |
|------|-----------|--------|
| `WEBUI_AUTH` default → `True` | Security: Open WebUI ships open by default | XS |
| MCP per-client policy enforcement | `allow_clients` metadata; needs upstream `X-Client-ID` | L (external dep) |
| Skill API keys in `openclaw.json` | Per-plugin; use upstream SecretRef where available | M |
| RBAC (read-only role) | View logs/health without start/stop access | L |

### M6 Acceptance Criteria

- **Given** `docker compose up -d`, **When** env does not set `WEBUI_AUTH`, **Then** Open WebUI requires login
- **Given** `docker inspect mcp-gateway`, **Then** `NetworkSettings.Networks` contains only `ordo-ai-stack-backend`
- **Given** audit log exceeds 10MB, **When** next privileged action occurs, **Then** old log renamed to `audit.log.1`
- **Given** push to main branch, **When** CI runs, **Then** all contract + smoke tests pass

---

## M7 — Reliability Spine

**Outcome:** When OpenClaw or any client fails, operators can tell **which hop** failed and whether the failure is **retryable** or **operator-action-required**.

**Phase 1 (failures visible):** Typed `/health` and `/ready` for model gateway, MCP gateway, and browser bridge; dependency registry in config + dashboard surface; `X-Request-ID` / correlation end-to-end; failure taxonomy; dashboard dependency status; OpenClaw startup validation; smoke tests.

**Phase 2 (degradation & recovery):** Provider fallback chains; per-tool / per-server circuit breakers; cold/warm model state; standardized timeout & retry budgets; auto-disable/quarantine unhealthy tools; ops-controller restart hooks; browser bridge session health / recycle.

**Phase 3 (operator-grade):** SLO dashboard; version-pinned bundles; rollback; `BASE_PATH` backup/restore; config migration engine; expanded integration test matrix.

**Explicit non-goals:** Monolithic OpenClaw fork; dashboard as required runtime dependency; ops-controller in hot path; new services before contracts harden; "restart fixes it" as primary strategy.

---

## Test Plan (Current)

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
