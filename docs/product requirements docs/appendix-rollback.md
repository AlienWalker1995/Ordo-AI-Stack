# Appendix: Rollback Procedures

1. **Model gateway:** Point services directly to llama.cpp (`OPENAI_API_BASE=http://llamacpp:8080/v1`); `docker compose stop model-gateway`. Restart affected services.
2. **Ops controller:** Remove controller from compose or set no token; ops buttons show "unavailable" in dashboard. No data loss.
3. **MCP registry:** Delete `registry.json`; dashboard falls back to `servers.txt` only. Policy metadata disabled.
4. **cap_drop / read_only:** Remove from compose; `docker compose up -d --force-recreate <service>`.
5. **Reset OPS_CONTROLLER_TOKEN:** `openssl rand -hex 32` → update `.env` → `docker compose up -d dashboard ops-controller`.
6. **MCP tools:** Clear `data/mcp/servers.txt` or set to single safe server → gateway hot-reloads within 10s.
7. **RAG:** `docker compose stop rag-ingestion qdrant`; remove `VECTOR_DB=qdrant` from Open WebUI env → Open WebUI uses built-in vector store. Qdrant data preserved in `data/qdrant/`.
8. **Invalidate model cache:** `curl -X DELETE http://localhost:11435/v1/cache` — forces fresh fetch from llama.cpp on next `/v1/models` call.
9. **Safe mode:** `docker compose stop mcp-gateway hermes-gateway comfyui rag-ingestion` → llama.cpp + Open WebUI + dashboard only.
