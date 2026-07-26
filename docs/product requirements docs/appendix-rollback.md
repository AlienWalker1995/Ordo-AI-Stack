# Appendix: Rollback Procedures

1. **Model gateway:** Point services directly to llama.cpp (`OPENAI_API_BASE=http://llamacpp:8080/v1`); `cd out && docker compose -p ordo stop model-gateway`. Restart affected services.
2. **Ops API:** Remove `ops-api` from compose or set no token; ops buttons show "unavailable" in dashboard. No data loss. (The auth-free `ordo serve` scheduler on ops-controller is separate and unaffected.)
3. **MCP registry:** Delete `registry-custom.yaml`; the gateway falls back to `servers.txt` only. Policy metadata disabled.
4. **cap_drop / read_only:** Remove from compose; `cd out && docker compose -p ordo up -d --force-recreate <service>`.
5. **Reset OPS_CONTROLLER_TOKEN:** `openssl rand -hex 32` → update `out/secrets.env` → `cd out && docker compose -p ordo up -d dashboard ops-api agent` (ops-api enforces the Bearer; the dashboard and Hermes hold it as clients — the `ordo serve` scheduler on ops-controller is deliberately auth-free and needs no restart).
6. **MCP tools:** Clear `data/mcp/servers.txt` or set to single safe server → gateway hot-reloads within 10s.
7. **RAG:** `cd out && docker compose -p ordo stop rag-ingestion qdrant`; remove `VECTOR_DB=qdrant` from Open WebUI env → Open WebUI uses built-in vector store. Qdrant data preserved in `data/qdrant/`.
8. **Invalidate model cache** (model-gateway has no host port — go in-network or via the Caddy `/llm` edge): `docker compose -p ordo exec dashboard curl -X DELETE http://model-gateway:11435/v1/cache` (or `curl -X DELETE -H "Authorization: Bearer $LITELLM_MASTER_KEY" https://<host>/llm/v1/cache`) — forces fresh fetch from llama.cpp on next `/v1/models` call.
9. **Safe mode:** `cd out && docker compose -p ordo stop mcp-gateway agent comfyui rag-ingestion` → llama.cpp + Open WebUI + dashboard only.
