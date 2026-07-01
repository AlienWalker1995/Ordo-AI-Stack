# Appendix: Environment Variables Reference

| Variable | Service | Description | Default |
|----------|---------|-------------|---------|
| `BASE_PATH` | compose | Project root path | `.` |
| `DATA_PATH` | compose | Data directory | `${BASE_PATH}/data` |
| `LLAMACPP_URL` | model-gateway, dashboard | llama.cpp internal URL | `http://llamacpp:8080` |
| `MODEL_CACHE_TTL_SEC` | model-gateway | Model list cache TTL seconds | `60` |
| `DASHBOARD_URL` | model-gateway | Dashboard for throughput recording | `http://dashboard:8080` |
| `OPS_CONTROLLER_URL` | dashboard | Ops controller URL | `http://ops-controller:9000` |
| `OPS_CONTROLLER_TOKEN` | dashboard, ops-controller | Bearer token for ops API | *(required)* |
| `DASHBOARD_AUTH_TOKEN` | dashboard | Bearer token for dashboard API | *(optional)* |
| `DEFAULT_MODEL` | dashboard, open-webui | Default model shown in Open WebUI chat | *(optional)* |
| `HERMES_DASHBOARD_PORT` | hermes-dashboard | Hermes dashboard host port | `9119` |
| `DISCORD_BOT_TOKEN` | hermes-gateway | Discord bot token | *(optional)* |
| `DISCORD_ALLOWED_USERS` | hermes-gateway | Comma-separated Discord user IDs authorized to DM/invoke | *(required for Discord use)* |
| `MCP_GATEWAY_PORT` | mcp-gateway | MCP gateway host port | `8811` |
| `MODEL_GATEWAY_PORT` | model-gateway | Model gateway host port | `11435` |
| `WEBUI_AUTH` | open-webui | Enable Open WebUI auth | `False` (target `True` in M6) |
| `OPENAI_API_BASE` | open-webui, n8n | OpenAI-compat base URL | `http://model-gateway:11435/v1` |
| `GGUF_MODELS` | gguf-puller | Hugging Face repo(s) of GGUF files to pull | *(empty)* |
| `COMPUTE_MODE` | compose | CPU/nvidia/amd | auto-detected |
| `QDRANT_PORT` | qdrant | Qdrant host port | `6333` |
| `EMBED_MODEL` | rag-ingestion | Embedding model for RAG | `nomic-embed-text` |
| `RAG_COLLECTION` | rag-ingestion, dashboard | Qdrant collection name | `documents` |
| `RAG_CHUNK_SIZE` | rag-ingestion | Token chunk size for document splitting | `400` |
| `RAG_CHUNK_OVERLAP` | rag-ingestion | Token overlap between chunks | `50` |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | mcp-gateway | GitHub MCP server token | *(optional)* |
