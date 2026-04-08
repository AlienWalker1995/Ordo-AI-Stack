# Model Gateway

LiteLLM proxy in front of the local `llamacpp` and `llamacpp-embed` services.

## Endpoints

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/messages`
- `POST /v1/responses`
- `POST /v1/embeddings`
- `GET /health`

## Config

The runtime config lives in [`litellm_config.yaml`](./litellm_config.yaml). The compose service passes:

- `LLAMACPP_URL`
- `LLAMACPP_EMBED_URL`
- `CLAUDE_CODE_LOCAL_MODEL`

The container image is based on `ghcr.io/berriai/litellm:main-stable`.
