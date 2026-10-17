# Ordo AI Stack — Product Requirements Document

Reference docs for the stack's components and cross-cutting design. Operator how-tos live in [`../operator-guide.md`](../operator-guide.md) and [`../runbooks/`](../runbooks/).

---

## Product Vision

A self-hosted AI platform that any developer can run by rendering `ordo.yaml` (`ordo render`) and bringing it up with `ordo up --all` from the repo root. Core guarantees:

1. **One model endpoint** — Every service reaches every model served by the GPU inference engine (llama.cpp or NInfer) via a single OpenAI-compatible gateway. No per-service provider config.
2. **Shared tools with health** — MCP tools served from one `/mcp` endpoint on the model gateway, with per-server health badges and per-consumer key scoping.
3. **Authenticated ops** — Dashboard manages the full service lifecycle through a secure, audited control plane. No docker.sock in the UI layer.
4. **RAG out of the box** — Vector search (Qdrant) is wired into Open WebUI and exposed to the gateway; document ingestion is one compose profile away.
5. **Hardened by default** — Non-root containers, `cap_drop: [ALL]`, read-only filesystems, explicit networks, log rotation, resource limits across all custom services.

---

## Component Docs

- [Architecture & Principles](architecture-and-principles.md) – System architecture, product principles, data flows, network assignments.
- [Model Gateway](../../services/model-gateway/README.md) – The LiteLLM gateway: model list, aliases, CPU failover, cost, MCP aggregation.
- [Ops Controller](component-ops-controller.md) – Docker Compose control plane: GPU scheduler, model switch, lifecycle API (internal port 9000).
- [MCP & Tool Aggregation](component-mcp-gateway.md) – One `/mcp` endpoint on the model gateway; the `mcp-*` servers behind it, scoped per LiteLLM key.
- [RAG Pipeline](component-rag-pipeline.md) – Qdrant vector search + document ingestion.
- [Orchestration Layer](component-orchestration-layer.md) – Multi-service workflow coordination (target architecture; implementation evolves with the repo).
- [Dashboard UI](component-dashboard-ui.md) – Ops dashboard: Overview, Services, Models and Performance pages.
- [Security & Trust Model](security-and-trust-model.md) – Threat model, auth tiers, SSRF, secret handling.
- [Environment Variables Reference](appendix-env-vars.md) – All env vars, services, defaults.
- [Rollback Procedures](appendix-rollback.md) – Per-component rollback playbook.
- [Open WebUI](https://docs.openwebui.com/) – Day-to-day chat interface (port 3000); not owned by this repo but wired into the stack via compose.

