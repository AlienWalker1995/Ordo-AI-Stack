# Appendix: Quality Bar

## Test Suite (Current `tests/`)

57 test files: 34 in `tests/` plus 23 in `tests/substrate/` (the render-engine suite).

| Area | Files | Representative coverage |
|------|-------|--------------------------|
| Dashboard | 8 | health, auth middleware, proxy auth, registry routes, service pressure, ComfyUI packs, dependencies, performance |
| ComfyUI | 5 | API client, workflow manager defaults, queue prompt (+ integration), default model env |
| Orchestration | 5 | API, e2e, outbox, workflow versioning, desk-influencer workflow |
| RAG | 2 | ingestion chunking/embedding, status |
| Ops / secrets / stack | 6 | secrets isolation, Caddyfile invariants, stack-monitor sanitize/versions, storage purge, settings validation |
| GPU / hardware | 3 | GPU stats, GPU routes, llama.cpp turboquant |
| Misc / policy | 5 | dependency-registry probe, Hermes socket absent, MCP policy, services & throughput, text sanitizers |
| Substrate (render engine) | 23 | agents, broker, cloud fallback, compose (+ recreate), control, dashboards, fetch, lease exec/history, MCP, memory vault (+ ingestion), native, ops-api stats, parity (+ render), plugins, preflight, render, scheduler, status doctor, wizard |

CI (`.github/workflows/ci.yml`): `secret-scan` (TruffleHog), `pytest` (`tests/`, excluding `tests/substrate/`), and `substrate` (path-gated, mocked-profile).

**Missing:**
- `test_responses_api.py` — Responses API format, tool conversion

## Performance Targets

- Model list (cached): `<100ms` after first call
- Model list (cold): `<2s` when llama.cpp healthy
- RAG embedding: `<5s` per document chunk (depends on model)
- Tool invocation: `<30s` default timeout
- Ops restart: `<60s` for most services
- Dashboard health: `<500ms`

## Security Review Checklist (Per PR)

- [ ] No secrets introduced in code or compose (check `git diff` for tokens)
- [ ] New services: non-root user, `cap_drop`, `security_opt`, log rotation, resource limits
- [ ] New endpoints: auth required for mutating operations
- [ ] New MCP tools: `allow_clients` explicitly set in registry
- [ ] No new host port exposures without justification
- [ ] Audit events emitted for all privileged actions
- [ ] New env vars documented in [Environment Variables Reference](appendix-env-vars.md) and `.env.example`

## Break-Glass Procedures

1. Reset admin token: see [Rollback Procedures](appendix-rollback.md) #5
2. Restore data: `rsync -a <backup>/data/ data/`; `docker compose up -d`
3. Disable all tools: `echo "" > data/mcp/servers.txt`
4. Invalidate model cache: `curl -X DELETE http://localhost:11435/v1/cache`
5. Disable unsafe services (from `out/`): `docker compose -p ordo stop mcp-gateway agent comfyui rag-ingestion`
6. Safe mode: `docker compose up -d llamacpp model-gateway dashboard open-webui qdrant`
