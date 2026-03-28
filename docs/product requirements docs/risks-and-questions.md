# Risks & Open Questions

## Risk Register

| Risk | Impact | Mitigation | Rollback |
|------|--------|------------|---------|
| `read_only: true` breaks model-gateway or dashboard | Service crash if writes to unexpected paths | Add `tmpfs: [/tmp]`; test with `docker compose up` before merging | Remove `read_only: true` from affected service |
| `cap_drop: [ALL]` breaks N8N or ComfyUI | Service fails if needing capabilities | Apply to custom-build services first; test third-party separately; add `cap_add` as needed | Remove `cap_drop` from affected service |
| ops-controller user change breaks docker.sock access | 403 on all docker operations | Verify docker group GID on host; set `user: "1000:<gid>"` | Revert user to root temporarily |
| Model gateway cache serves stale model list | Users see deleted models | Cache TTL is 60s; `DELETE /v1/cache` to invalidate | Set `MODEL_CACHE_TTL_SEC=0` to disable cache |
| WEBUI_AUTH=True breaks existing setups | Users locked out of Open WebUI | Document in UPGRADE.md; `WEBUI_AUTH=False` to opt out | `WEBUI_AUTH=False` in `.env` |
| docker.sock in two services | Two attack surfaces for container escape | Accept: both required. Mitigate with allowlists, auth, no host ports | Remove one; document trade-off |
| MCP filesystem SSRF | Tool access to host filesystem | Removed from default; `allow_clients: []` in registry | Clear from servers.txt |
| Prompt injection via MCP tool output | Model manipulated by tool results | Allowlists; structured output in tool_result tags; monitor | Remove suspicious tool from servers.txt |
| openclaw.json plaintext tokens on disk | Local token exposure if data/ is shared | Tokens in gitignored `data/`; document backup warnings | Rotate tokens |
| Performance regression from gateway proxy | >10ms added latency | Thin async proxy; benchmarked acceptable. Cache helps | Direct `OLLAMA_BASE_URL` escape hatch |

## Open Questions

| # | Question | Status |
|---|----------|--------|
| 1 | **Ops-controller docker GID:** `user: "1000:<gid>"` value depends on host docker GID | Resolved — ops-controller runs without explicit user |
| 2 | **Open WebUI `OPENAI_API_BASE`:** Does `open-webui:v0.8.4` support this env? | Resolved — uses `OPENAI_API_BASE_URL`; working |
| 3 | **MCP gateway policy:** Does Docker MCP Gateway support `X-Client-ID` for per-client allowlist? | Open — not yet; deferred to M6 |
| 4 | **openclaw.json token externalization:** Can `merge_gateway_config.py` inject tokens from env? | Resolved for gateway auth + Discord/Telegram |
| 5 | **Ollama host port:** Remove to reduce attack surface? | Resolved — backend-only by default; `overrides/ollama-expose.yml` |
| 6 | **Audit log rotation** | Resolved — size-based rotation (`AUDIT_LOG_MAX_BYTES`) |
| 7 | **vLLM timing** | Resolved — `overrides/vllm.yml` with `--profile vllm` |
| 8 | **ComfyUI non-root** | Open — `yanwk/comfyui-boot` runs as root; image limitation |
| 9 | **Smoke test in CI** | Resolved — see `.github/workflows/ci.yml` |
| 10 | **N8N LLM node** | Open — use OpenAI-compat node with `baseURL: http://model-gateway:11435/v1`; needs example workflow doc |
| 11 | **RAG embed model pull** | Open — `nomic-embed-text` must be pulled before ingestion; add to model-puller default list or document |
| 12 | **Reliability spine (M7)** | Partial — registry + health/ready + doctor/validation shipped; circuit breakers / full L3 semantics remain |
