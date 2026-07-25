# Appendix: Environment Variables Reference

| Variable | Service | Description | Default |
|----------|---------|-------------|---------|
| `BASE_PATH` | compose | Project root path | `.` |
| `DATA_PATH` | compose | Data directory | `${BASE_PATH}/data` |
| `LLAMACPP_URL` | model-gateway, dashboard | llama.cpp internal URL | `http://llamacpp:8080` |
| `MODEL_CACHE_TTL_SEC` | model-gateway | Model list cache TTL seconds | `60` |
| `DASHBOARD_URL` | model-gateway | Dashboard for throughput recording | `http://dashboard:8080` |
| `OPS_CONTROLLER_URL` | dashboard | Deliberately overridden for the V1-parity dashboard to point at the `ops-api` backend, not the V2 scheduler (`services/v1-parity/dashboard.yaml`) | `http://ops-api:9000` |
| `OPS_CONTROLLER_URL` | agent, mcp-gateway, hermes-dashboard, comfyui, comfyui-mcp, ltx-trainer | V2 scheduler (`ops-controller`) URL for GPU leases | `http://ops-controller:9000` |
| `OPS_CONTROLLER_TOKEN` | dashboard, ops-controller | Bearer token for ops API | *(required)* |
| `DASHBOARD_AUTH_TOKEN` | dashboard | Optional dormant Bearer-auth fallback in dashboard code; not set in the Ordo deployment — the Caddy edge (oauth2-proxy + Google SSO) is the sole auth gate for the dashboard | *(unset; not used)* |
| `DEFAULT_MODEL` | dashboard, open-webui | Default model shown in Open WebUI chat | *(optional)* |
| `HERMES_DASHBOARD_PORT` | hermes-dashboard | Not wired to anything — the dashboard's listen port is hardcoded via `--port 9119` in `services/hermes-dashboard/plugin.yaml`; documented here for reference only | `9119` |
| `DISCORD_BOT_TOKEN_FILE` | agent | Docker secret file path for the Discord bot token; the entrypoint reads it into `DISCORD_BOT_TOKEN` inside the container. Plaintext `DISCORD_BOT_TOKEN` env is never set — `tests/test_secrets_isolation.py` asserts it's absent | `/run/secrets/discord_token` |
| `DISCORD_ALLOWED_USERS` | agent | Comma-separated Discord user IDs authorized to DM/invoke | *(required for Discord use)* |
| `MCP_GATEWAY_PORT` | mcp-gateway | Internal port the gateway listens on (`ordo-net` only; no host publish — only Caddy publishes a port) | `8811` |
| `MODEL_GATEWAY_PORT` | model-gateway | Not wired to anything — `services/model-gateway/entrypoint.sh` hardcodes `--port 11435`; documented here for reference only | `11435` |
| `WEBUI_AUTH` | open-webui | Enable Open WebUI auth | `False` (target `True` in M6) |
| `OPENAI_API_BASE` | open-webui, n8n | OpenAI-compat base URL | `http://model-gateway:11435/v1` |
| `GGUF_MODELS` | gguf-puller | Hugging Face repo(s) of GGUF files to pull | *(empty)* |
| `COMPUTE_MODE` | compose | CPU/nvidia/amd | auto-detected |
| `QDRANT_PORT` | qdrant | Not wired to anything — consumers hardcode `http://qdrant:6333` (e.g. `services/qdrant-rag/plugin.yaml`); no host publish (`ordo-net` only) | `6333` |
| `EMBED_MODEL` | rag-ingestion | Embedding model for RAG | `nomic-embed-text` |
| `RAG_COLLECTION` | rag-ingestion, dashboard | Qdrant collection name | `documents` |
| `RAG_CHUNK_SIZE` | rag-ingestion | Token chunk size for document splitting | `400` |
| `RAG_CHUNK_OVERLAP` | rag-ingestion | Token overlap between chunks | `50` |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | mcp-gateway | GitHub MCP server token | *(optional)* |
