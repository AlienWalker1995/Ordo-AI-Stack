# Component: Model Gateway

## Purpose
- Central hub for local model access, fronting the llama.cpp inference server.
- Provides unified model execution, token management, and cross-model communication.
- Acts as a bridge between services, enabling them to call each other's APIs or workflows.

## Key Responsibilities
- **Unified API**: OpenAI-compatible surface (`/v1/...`) for local and routed models.
- **Provider / API keys**: Manages keys and headers where configured; local llama.cpp uses the stack's shared key material.
- **Cross-service use**: Open WebUI, Hermes, n8n, and other services target this service instead of raw llama.cpp where compose wires them.
- **Extensibility**: Additional backends or policies are added in the gateway service code and compose env—not in every client.

## API Reference

**Base URL:** `http://model-gateway:11435`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/models` | GET | Model list from llama.cpp; TTL-cached 60s |
| `/v1/chat/completions` | POST | Chat; streaming; tool-calling |
| `/v1/responses` | POST | OpenAI Responses API — converts to chat completions + tools; streams |
| `/v1/completions` | POST | Legacy completions compat — wraps chat completions |
| `/v1/embeddings` | POST | Embeddings via llama.cpp |
| `/v1/cache` | DELETE | Invalidate model list cache (force re-fetch from llama.cpp) |
| `/health` | GET | Gateway health; checks at least one provider reachable |
| `/ready` | GET | Readiness; verifies model list available |

### Model Naming

- Model ids map to the GGUF served by llama.cpp (typically the GGUF basename); no provider prefix is required.

### Headers

- `X-Service-Name: <caller>` — for throughput attribution
- `X-Request-ID: <uuid>` — for correlation

### Responses API Notes

Converts Responses API input items and tool definitions to chat-completions format. Tool calls in Responses API format (`function` type with `parameters`) are re-serialized back to Responses format in the response. Unsupported tool types (e.g. `computer_use_preview`) are filtered before forwarding.

## Provider Abstraction (LiteLLM)

- LiteLLM proxy (config in `model-gateway/litellm_config.yaml`) fronts the local `llamacpp` and `llamacpp-embed` services; both speak the OpenAI-compatible API natively, so requests proxy directly.
- TTL model list cache (60s default; stale-serve on provider error)
- `DELETE /v1/cache` to invalidate cache on demand
- `X-Request-ID` generated or forwarded on every chat/embeddings call
- Responses API (`/v1/responses`) with tool-call pass-through
- Completions compat (`/v1/completions`)

## Client Compatibility

| Client | Current | Target | Change needed |
|--------|---------|--------|---------------|
| Open WebUI | `OPENAI_API_BASE_URL=http://model-gateway:11435/v1` | Same | None |
| Hermes | `http://model-gateway:11435/v1` | Same | None |
| N8N | No LLM node set | `OPENAI_API_BASE=http://model-gateway:11435/v1` | Docs only |
| Cursor/external | `http://localhost:11435/v1` | Same | None |

## Configuration

```yaml
# docker-compose.yml (current)
model-gateway:
  environment:
    - LLAMACPP_URL=http://llamacpp:8080
    - LLAMACPP_EMBED_URL=http://llamacpp-embed:8080
    - CLAUDE_CODE_LOCAL_MODEL=${CLAUDE_CODE_LOCAL_MODEL:-}
    - DASHBOARD_URL=http://dashboard:8080
```

## Non-Goals
- Direct UI rendering. UI components are separate and consume the gateway.
- Persistent storage of model results — the gateway only forwards results.

## Dependencies
- Docker service **`model-gateway`** (`model-gateway/litellm_config.yaml`, compose env such as `LLAMACPP_URL`, `MODEL_GATEWAY_URL` for consumers).
- Root **`.env`** / compose for llama.cpp attachment and context limits.
