# Model Gateway

> ⚠️ **LEGACY — superseded by [`v2/docker/model-gateway/`](../v2/docker/model-gateway/).** Since the 2026-07-09 cutover, the production stack builds the model gateway (`ordo-v2/model-gateway:latest`, the LiteLLM + `local-chat` alias config-wrapper) from `v2/docker/model-gateway/`, not this directory. This root copy is retained pending a separate cleanup PR — see [`docs/LEGACY-CLEANUP.md`](../docs/LEGACY-CLEANUP.md). It still documents the gateway's endpoints/config accurately, but is no longer what runs.

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
