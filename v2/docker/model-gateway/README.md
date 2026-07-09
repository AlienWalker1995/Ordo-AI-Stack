# model-gateway (LiteLLM config-wrapper)

V2's `model-gateway` core service. This is the small config-wrapper build V1 runs
(`ordo-ai-stack-model-gateway:latest`) — a pinned LiteLLM base plus the stack's config: the
canonical **`local-chat`** alias, the `local-embed` alias, the throughput callback, and the
entrypoint that templates `__CTX_SIZE__` / `__MASTER_KEY__` at startup.

V2 references it as a **project buildable image** (`ordo-v2/model-gateway:latest`) — pinned by
its build context, not pulled from a registry — so `ordo preflight` reports a missing one as
"build first", never "Docker will pull". This is why V2 does NOT reference the unconfigured
upstream `ghcr.io/berriai/litellm:main` directly: that image has no `local-chat` alias.

## Build
```
docker build -t ordo-v2/model-gateway:latest v2/docker/model-gateway
```

## Files
- `Dockerfile` — pins `ghcr.io/berriai/litellm:main-stable`, installs the config + callback.
- `litellm_config.yaml` — the `local-chat` / `local-embed` model list (no secrets; `__MASTER_KEY__`
  and `__CTX_SIZE__` are entrypoint-substituted at runtime).
- `entrypoint.sh` — renders the template with the rendered ctx + master key.
- `throughput_callback.py` — posts per-completion tok/s + TTFT samples to the dashboard.

`LITELLM_MASTER_KEY` and `THROUGHPUT_RECORD_TOKEN` are supplied at runtime from the
operator-managed `secrets.env` (never baked).
