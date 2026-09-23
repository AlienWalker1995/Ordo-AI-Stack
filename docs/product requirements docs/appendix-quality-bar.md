# Appendix: Quality Bar

## Test Suite (Current `tests/`)

87 test files: 42 in `tests/` plus 45 in `tests/substrate/` (the render-engine suite).

| Area | Files | Representative coverage |
|------|-------|--------------------------|
| Dashboard | 8 | health, auth middleware, proxy auth, console (+ routes), retired routes, service pressure, throughput record/stats |
| ComfyUI | 5 | workflow manager defaults, queue prompt (+ integration), default model env, comfyui-mcp management tools |
| Orchestration | 4 | API, MCP tools, workflows, workflow versioning |
| RAG | 2 | ingestion chunking/embedding, status |
| Ops / secrets / stack | 11 | secrets isolation, Caddyfile invariants, stack-monitor sanitize/versions/pinned sources, storage purge, settings validation, service-catalog wiring + fragments, service lifecycle, monitoring config |
| GPU / hardware | 3 | GPU stats, gpu-gate, llama.cpp KV-cache args |
| Misc / policy | 9 | Hermes socket absent, Hermes ops client, Hermes ordo skills, MCP policy, MCP persist, services & throughput, throughput callback, text sanitizers, vault federate |
| Substrate (render engine) | 45 | agents, backend protocol, bootstrap keys, bridge Dockerfiles, broker, build contexts, cli render guard, cloud fallback, compose (+ recreate), control (+ ASGI, routes), dashboards, evals, fetch, GPU arbitration, Langfuse (+ retention), lease exec/history, LiteLLM keys + Google SSO, local cost, MCP, memory vault (+ ingestion), model-gateway callbacks/merge, native, Obsidian LiveSync, ops-controller image, parity (+ render), plugin install, plugins, preflight, reference integrity, render, scheduler, service stats, env/images/comfy routes, source edit, status doctor, wizard |

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
- [ ] New MCP servers: `kind: mcp` manifest with an explicit `network:` and only the `env:` / `secrets:` the server needs; granted to the keys that should see it
- [ ] No new host port exposures without justification
- [ ] Audit events emitted for all privileged actions
- [ ] New env vars documented in [Environment Variables Reference](appendix-env-vars.md), `ordo.example.yaml`, and `out/secrets.env.example`

## Break-Glass Procedures

1. Reset admin token: see [Rollback Procedures](appendix-rollback.md) #5
2. Restore data: `rsync -a <backup>/data/ data/`; `docker compose up -d`
3. Disable all tools: remove the `kind: mcp` plugins from `ordo.yaml`'s `plugins:` list, then `ordo render` and recreate `model-gateway`
4. Invalidate model cache (model-gateway has no host port — go in-network or via the Caddy `/llm` edge): `docker compose -p ordo exec dashboard curl -X DELETE http://model-gateway:11435/v1/cache` (or `curl -X DELETE -H "Authorization: Bearer $LITELLM_MASTER_KEY" https://<host>/llm/v1/cache`)
5. Disable unsafe services (from `out/`): `docker compose -p ordo stop $(docker compose -p ordo config --services | grep '^mcp-') agent comfyui rag-ingestion`
6. Safe mode: `docker compose up -d llamacpp model-gateway dashboard open-webui qdrant`
