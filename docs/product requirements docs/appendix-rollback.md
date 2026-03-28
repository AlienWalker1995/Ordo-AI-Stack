# Appendix: Rollback Procedures

1. **Model gateway:** Point services directly to Ollama (`OLLAMA_BASE_URL=http://ollama:11434`); `docker compose stop model-gateway`. Restart affected services.
2. **Ops controller:** Remove controller from compose or set no token; ops buttons show "unavailable" in dashboard. No data loss.
3. **MCP registry:** Delete `registry.json`; dashboard falls back to `servers.txt` only. Policy metadata disabled.
4. **cap_drop / read_only:** Remove from compose; `docker compose up -d --force-recreate <service>`.
5. **Reset OPS_CONTROLLER_TOKEN:** `openssl rand -hex 32` → update `.env` → `docker compose up -d dashboard ops-controller`.
6. **Reset OPENCLAW_GATEWAY_TOKEN:** Update `.env` → `docker compose restart openclaw-gateway` → re-pair clients.
7. **MCP tools:** Clear `data/mcp/servers.txt` or set to single safe server → gateway hot-reloads within 10s.
8. **RAG:** `docker compose stop rag-ingestion qdrant`; remove `VECTOR_DB=qdrant` from Open WebUI env → Open WebUI uses built-in vector store. Qdrant data preserved in `data/qdrant/`.
9. **Invalidate model cache:** `curl -X DELETE http://localhost:11435/v1/cache` — forces fresh fetch from Ollama on next `/v1/models` call.
10. **Safe mode:** `docker compose stop mcp-gateway openclaw-gateway comfyui rag-ingestion` → Ollama + Open WebUI + dashboard only.
