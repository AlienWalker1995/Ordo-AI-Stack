# Appendix: Environment Variables Reference

Service-wiring variables the render sets. Operator-set variables (paths, ports, RAG tuning, tokens) are in [configuration.md](../configuration.md).

| Variable | Service | Description | Default |
|----------|---------|-------------|---------|
| `LLAMACPP_URL` | model-gateway, dashboard | llama.cpp internal URL | `http://llamacpp:8080` |
| `DASHBOARD_URL` | model-gateway | Dashboard for throughput recording | `http://dashboard:8080` |
| `OPS_CONTROLLER_URL` | dashboard, agent, comfyui, mcp-comfyui, ltx-trainer | Control plane (`ops-controller`) URL: GPU leases, model switch, service lifecycle | `http://ops-controller:9000` |
| `OPEN_WEBUI_DEFAULT_MODEL` | open-webui | Default model shown in Open WebUI chat (rendered into `DEFAULT_MODELS`) | `local-chat` |
| `DISCORD_BOT_TOKEN_FILE` | agent | Docker secret file path for the Discord bot token; the entrypoint reads it into `DISCORD_BOT_TOKEN` inside the container. Plaintext `DISCORD_BOT_TOKEN` env is never set; `tests/test_secrets_isolation.py` asserts it's absent | `/run/secrets/discord_bot_token` |
| `MCP_SERVERS_PATH` | dashboard | Rendered MCP server list read by the dashboard (`./mcp:/mcp-config:ro`) | `/mcp-config/servers.json` |
| `WEBUI_AUTH` | open-webui | Enable Open WebUI auth | `False` (target `True` in M6) |
| `OPENAI_API_BASE` | open-webui | OpenAI-compat base URL | `http://model-gateway:11435/v1` |
| `LITELLM_MASTER_KEY` | model-gateway, model-gateway-keys, dashboard | LiteLLM master key; the entrypoint refuses to start unless it matches `^sk-[A-Za-z0-9_-]{32,}$` | *(required)* |
| `LITELLM_SALT_KEY` | model-gateway | Encrypts provider credentials stored in `litellm-db`. NEVER rotate | *(required)* |
| `LITELLM_DB_PASSWORD` | model-gateway, litellm-db | Postgres password, a file for both (`POSTGRES_PASSWORD_FILE`, `DATABASE_PASSWORD_FILE`) | *(required)* |
| `LITELLM_KEY_HERMES` / `_OPEN_WEBUI` / `_EDGE` | agent, open-webui, external clients | Per-consumer LiteLLM virtual keys, provisioned by `model-gateway-keys` | *(generated)* |
| `LITELLM_KEYS_SPEC` | model-gateway-keys | Rendered key/grant spec read by the bootstrap one-shot | `/config/keys.json` |
| `LITELLM_MODE` | model-gateway | LiteLLM run mode | `PRODUCTION` |
| `STORE_MODEL_IN_DB` | model-gateway | Models and MCP servers stay in the rendered config, never in the DB | `False` |
| `DATABASE_HOST` / `DATABASE_USERNAME` / `DATABASE_NAME` | model-gateway | LiteLLM builds its Postgres DSN from these and `DATABASE_PASSWORD` (exported from its file by the entrypoint) | *(rendered)* |
