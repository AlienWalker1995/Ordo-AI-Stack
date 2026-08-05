# Ordo AI Stack — Product Requirements Document

**Status:** Living document — updated 2026-03-28
**Scope:** Local-first AI platform: unified model access, shared tools, secure ops, RAG, and agentic runtime.

---

## Product Vision

A self-hosted AI platform that any developer can run by rendering `ordo.yaml` (`ordo render`) and bringing it up with `docker compose -p ordo … up -d` from `out/`. Core guarantees:

1. **One model endpoint** — Every service reaches every model served by llama.cpp via a single OpenAI-compatible gateway. No per-service provider config.
2. **Shared tools with health** — MCP tools served from a central gateway with registry metadata, per-server health badges, and policy controls.
3. **Authenticated ops** — Dashboard manages the full service lifecycle through a secure, audited control plane. No docker.sock in the UI layer.
4. **RAG out of the box** — Vector search (Qdrant) is wired into Open WebUI and exposed to the gateway; document ingestion is one compose profile away.
5. **Hardened by default** — Non-root containers, `cap_drop: [ALL]`, read-only filesystems, explicit networks, log rotation, resource limits across all custom services.

## Shipped Capabilities (as of 2026-03-04)

| Capability | Status | Key Files |
|-----------|--------|-----------|
| OpenAI-compat model gateway (llama.cpp) | Live | `services/model-gateway/` |
| Model list TTL cache + cache-bust endpoint | Live | `services/model-gateway/` |
| `X-Request-ID` correlation end-to-end | Live | `services/model-gateway/`, `services/v1-parity/dashboard/app.py`, `ordo/` (`ordo serve`) |
| Responses API (`/v1/responses`) | Live | `services/model-gateway/` |
| Completions compat (`/v1/completions`) | Live | `services/model-gateway/` |
| MCP Gateway with hot-reload | Live | `services/mcp-gateway/`, `out/docker-compose.yml` |
| MCP registry metadata layer (`registry-custom.yaml`) | Live | `services/mcp-gateway/`, `data/mcp/registry-custom.yaml` |
| MCP health endpoint + UI badges | Live | `services/v1-parity/dashboard/app.py` |
| Ops API — container lifecycle (start/stop/restart/logs/pull) | Live | `services/ops-api/main.py` |
| Append-only JSONL audit log | Live | `services/ops-api/main.py`, `services/ops-api/audit.py` |
| Dashboard auth via Caddy edge SSO (oauth2-proxy + Google + allowlist); optional dormant per-service Bearer token in code, unused in deployment | Live | `services/v1-parity/dashboard/app.py` |
| Dashboard throughput stats + benchmark | Live | `services/v1-parity/dashboard/app.py` |
| Dashboard hardware stats | Live | `services/v1-parity/dashboard/app.py` |
| Dashboard default-model management | Live | `services/v1-parity/dashboard/app.py` |
| RAG pipeline (Qdrant + rag-ingestion) | Live | `services/rag/`, `out/docker-compose.yml` |
| Open WebUI → Qdrant vector DB | Live | `out/docker-compose.yml` |
| RAG status endpoint | Live | `services/v1-parity/dashboard/app.py` |
| Docker hardening (cap_drop, read_only, networks) | Live | `out/docker-compose.yml` |
| Single `ordo-net` Docker network (edge-only host-port publish) | Live | `out/docker-compose.yml` |
| llama.cpp backend-only (no host port) | Live | `out/docker-compose.yml` |
| SSRF egress block scripts | Live | `scripts/ssrf-egress-block.sh`, `.ps1` |
| Hermes agent (gateway + dashboard) | Live | `out/docker-compose.yml`, `services/hermes/` |
| Contract + smoke tests | Live | `tests/` |

## Open Risks

| Risk | Severity | Status |
|------|----------|--------|
| `docker.sock` in both `mcp-gateway` and `ops-controller` | High | Accepted — mitigated by allowlist + auth + no host port |
| `WEBUI_AUTH` still defaults to `False` | Medium | Tracked — change to `True` in M6 |
| MCP per-client policy (`allow_clients`) not enforced at gateway level | Medium | Planned — requires Docker MCP Gateway `X-Client-ID` support |
| Rendered compose validated in CI | Low | Done — `.github/workflows/ci.yml` has `secret-scan`, `pytest` (incl. ruff), and `substrate` jobs on push/PR; `substrate` runs `docker compose config` against the rendered output (path-filtered, no separate `compose-smoke` job) |
| Reliability / readiness contracts | High | Strategic — see [reliability-and-contracts.md](reliability-and-contracts.md) |

## Strategic Priority: Reliability Layer

The platform's next major quality bar is a **reliability spine**: guarantees that agent and tool clients can **reach, authenticate to, and recover from failures** across the shared stack—especially **Model Gateway `:11435`**, **MCP Gateway `:8811`**, and **browser/tool bridges** behind them.

**Design stance:** Any given agent (today: Hermes) is **one client** on a **shared service mesh**, not the architectural center. Service-to-service reliability and dependency management are the dominant failure mode when weak.

See [Reliability & Service Contracts](reliability-and-contracts.md) for full details.

---

## Component Docs

- [Architecture & Principles](architecture-and-principles.md) – System architecture, product principles, data flows, network assignments.
- [Model Gateway](component-model-gateway.md) – Unified model routing and provider-facing API keys (llama.cpp / OpenAI-compatible surface).
- [Ops Controller](component-ops-controller.md) – Secure Docker Compose control plane (token-auth lifecycle API, internal port 9000).
- [MCP & Tool Aggregation](component-mcp-gateway.md) – Single MCP entrypoint; ComfyUI / n8n / web tools via gateway.
- [RAG Pipeline](component-rag-pipeline.md) – Qdrant vector search + document ingestion.
- [Orchestration Layer](component-orchestration-layer.md) – Multi-service workflow coordination (target architecture; implementation evolves with the repo).
- [Dashboard UI](component-dashboard-ui.md) – Ops dashboard (Compose, models, workspace, MCP explorer).
- [Security & Trust Model](security-and-trust-model.md) – Threat model, auth tiers, SSRF, secret handling.
- [Reliability & Contracts](reliability-and-contracts.md) – Service contracts, health depth, circuit breakers, observability.
- [Milestones & Roadmap](milestones-and-roadmap.md) – M0–M7 milestone tracking, PR slices, acceptance criteria.
- [Risks & Open Questions](risks-and-questions.md) – Risk register and open questions.
- [Environment Variables Reference](appendix-env-vars.md) – All env vars, services, defaults.
- [Rollback Procedures](appendix-rollback.md) – Per-component rollback playbook.
- [Quality Bar](appendix-quality-bar.md) – Test suite, performance targets, security review checklist.
- [Open WebUI](https://docs.openwebui.com/) – Day-to-day chat interface (port 3000); not owned by this repo but wired into the stack via compose.

*Add or split components as the codebase grows.*
