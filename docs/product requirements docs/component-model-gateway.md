# Component: Model Gateway

## Purpose
- Central hub for multiple AI services (Ollama, OpenAI-compatible providers).
- Provides unified model execution, token management, and cross-model communication.
- Acts as a bridge between services, enabling them to call each other's APIs or workflows.

## Key Responsibilities
- **Unified API**: OpenAI-compatible surface (`/v1/...`) for local and routed models.
- **Provider / API keys**: Manages keys and headers for upstream providers where configured; local Ollama uses the stack's shared key material (e.g. `ollama-local` pattern).
- **Cross-service use**: Open WebUI, OpenClaw (via its `gateway` provider), n8n, and other services target this service instead of raw Ollama where compose wires them.
- **Extensibility**: Additional backends or policies are added in the gateway service code and compose env—not in every client.

## API Reference

**Base URL:** `http://model-gateway:11435`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/models` | GET | Aggregated model list (Ollama + vLLM); TTL-cached 60s |
| `/v1/chat/completions` | POST | Chat; routes by model prefix (`ollama/`, `vllm/`); streaming; tool-calling |
| `/v1/responses` | POST | OpenAI Responses API — converts to chat completions + tools; streams |
| `/v1/completions` | POST | Legacy completions compat — wraps chat completions |
| `/v1/embeddings` | POST | Embeddings; Ollama `/api/embed` + vLLM pass-through |
| `/v1/cache` | DELETE | Invalidate model list cache (force re-fetch from Ollama/vLLM) |
| `/health` | GET | Gateway health; checks at least one provider reachable |
| `/ready` | GET | Readiness; verifies model list available |

### Model Naming

- `ollama/deepseek-r1:7b` → Ollama
- `vllm/llama3` → vLLM (if `VLLM_URL` set)
- `deepseek-r1:7b` (no prefix) → `DEFAULT_PROVIDER`

### Headers

- `X-Service-Name: <caller>` — for throughput attribution
- `X-Request-ID: <uuid>` — for correlation

### Responses API Notes

Converts Responses API input items and tool definitions to chat-completions format. Tool calls in Responses API format (`function` type with `parameters`) are re-serialized back to Responses format in the response. Unsupported tool types (e.g. `computer_use_preview`) are filtered before forwarding.

## Provider Abstraction (`model-gateway/main.py`)

- `_model_provider_and_id(name)` → `(provider, model_id)` by prefix
- Ollama: translate to `/api/chat`, `/api/embed`; delta streaming
- vLLM: native OpenAI format; proxy directly
- TTL model list cache (60s default; stale-serve on provider error)
- `DELETE /v1/cache` to invalidate cache on demand
- `X-Request-ID` generated or forwarded on every chat/embeddings call
- Responses API (`/v1/responses`) with tool-call pass-through
- Completions compat (`/v1/completions`)

## Client Compatibility

| Client | Current | Target | Change needed |
|--------|---------|--------|---------------|
| Open WebUI | `OPENAI_API_BASE_URL=http://model-gateway:11435/v1` | Same | None |
| OpenClaw | `gateway` provider → `http://model-gateway:11435/v1` | Same | None |
| N8N | No LLM node set | `OPENAI_API_BASE=http://model-gateway:11435/v1` | Docs only |
| Cursor/external | `http://localhost:11435/v1` | Same | None |

## OpenClaw-Specific (Confirmed Working)

- `models.providers.gateway.baseUrl`: `http://model-gateway:11435/v1`
- `models.providers.gateway.api`: `openai-completions`
- `models.providers.gateway.headers.X-Service-Name`: `openclaw`
- Config sync: `merge_gateway_config.py` adds gateway provider if missing
- No migration needed — existing `ollama` provider continues to work

## Configuration

```yaml
# docker-compose.yml (current)
model-gateway:
  environment:
    - OLLAMA_URL=http://ollama:11434
    - VLLM_URL=${VLLM_URL:-}
    - DEFAULT_PROVIDER=ollama
    - DASHBOARD_URL=http://dashboard:8080
    - MODEL_CACHE_TTL_SEC=${MODEL_CACHE_TTL_SEC:-60}
```

### vLLM Compose Profile (Optional)

```yaml
# overrides/vllm.yml
services:
  vllm:
    profiles: [vllm]
    image: vllm/vllm-openai:latest
    ports:
      - "8000:8000"
    environment:
      - MODEL=${VLLM_MODEL:-meta-llama/Llama-3.2-3B-Instruct}
    deploy:
      resources:
        limits:
          memory: 16G
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

## Non-Goals
- Direct UI rendering. UI components are separate and consume the gateway.
- Persistent storage of model results — the gateway only forwards results.

## Dependencies
- Docker service **`model-gateway`** (`model-gateway/main.py`, compose env such as `OLLAMA_NUM_CTX`, `MODEL_GATEWAY_URL` for consumers).
- Root **`.env`** / compose for Ollama attachment and context limits.
