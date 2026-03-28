# Appendix: Quality Bar

## Test Suite (Current `tests/`)

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

## Performance Targets

- Model list (cached): `<100ms` after first call
- Model list (cold): `<2s` when Ollama healthy
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
5. Disable unsafe services: `docker compose stop mcp-gateway openclaw-gateway comfyui rag-ingestion`
6. Safe mode: `docker compose up -d ollama model-gateway dashboard open-webui qdrant`
