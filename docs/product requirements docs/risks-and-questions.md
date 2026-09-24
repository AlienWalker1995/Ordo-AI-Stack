# Risks & Open Questions

## Risk Register

| Risk | Impact | Mitigation | Rollback |
|------|--------|------------|---------|
| `read_only: true` breaks model-gateway or dashboard | Service crash if writes to unexpected paths | Add `tmpfs: [/tmp]`; test with `docker compose up` before merging | Remove `read_only: true` from affected service |
| `cap_drop: [ALL]` breaks N8N or ComfyUI | Service fails if needing capabilities | Apply to custom-build services first; test third-party separately; add `cap_add` as needed | Remove `cap_drop` from affected service |
| ops-controller user change breaks docker.sock access | 403 on all docker operations | Verify docker group GID on host; set `user: "1000:<gid>"` | Revert user to root temporarily |
| Model gateway cache serves stale model list | Users see deleted models | Cache TTL is 60s; `DELETE /v1/cache` to invalidate | Set `MODEL_CACHE_TTL_SEC=0` to disable cache |
| WEBUI_AUTH=True breaks existing setups | Users locked out of Open WebUI | Document the change in `CHANGELOG.md` / the operator guide; `WEBUI_AUTH=False` to opt out | `WEBUI_AUTH=False` in `ordo.yaml` (renders to `out/.env`) |
| docker.sock in two services | Two attack surfaces for container escape | Accept: both required. Mitigate with allowlists, auth, no host ports | Remove one; document trade-off |
| MCP filesystem access | Tool access to host filesystem | Each server declares its own `volumes:` (code root read-only, vault read-write); no server gets the gateway's env | Remove the plugin from `ordo.yaml`, `ordo render`, recreate `model-gateway` |
| Prompt injection via MCP tool output | Model manipulated by tool results | Per-key MCP grants; structured output in tool_result tags; monitor | Remove the suspicious server from `ordo.yaml`'s `plugins:`, `ordo render`, recreate `model-gateway` |
| Performance regression from gateway proxy | >10ms added latency | Thin async proxy; benchmarked acceptable. Cache helps | Point services directly at llama.cpp (`http://llamacpp:8080/v1`) escape hatch |

## Open Questions

| # | Question | Status |
|---|----------|--------|
| 1 | **Ops-controller docker GID:** `user: "1000:<gid>"` value depends on host docker GID | Resolved — ops-controller runs without explicit user |
| 2 | **Open WebUI `OPENAI_API_BASE`:** Does `open-webui` (running `v0.10.1`) support this env? | Resolved — uses `OPENAI_API_BASE_URL`; working |
| 3 | **MCP policy scoping:** How is per-consumer tool access enforced? | Resolved (2026-09): LiteLLM virtual-key `object_permission.mcp_servers` grants with `require_key_mcp_access_defined: true` |
| 5 | **llama.cpp host port:** Remove to reduce attack surface? | Resolved — backend-only; no host port |
| 6 | **Audit log rotation** | Resolved — size-based rotation in `ordo/audit.py` (10 MB, five generations kept) |
| 8 | **ComfyUI non-root** | Open — `yanwk/comfyui-boot` runs as root; image limitation |
| 9 | **Smoke test in CI** | Resolved — see `.github/workflows/ci.yml` |
| 10 | **N8N LLM node** | Open — use OpenAI-compat node with `baseURL: http://model-gateway:11435/v1`; needs example workflow doc |
| 11 | **RAG embed model pull** | Open, `nomic-embed-text` must be pulled before ingestion; fetch it with `ordo fetch` or document it |
| 12 | **Reliability spine (M7)** | Partial — registry + health/ready + doctor/validation shipped; circuit breakers / full L3 semantics remain |
