# model-gateway (LiteLLM config-wrapper)

The Ordo stack's `model-gateway` core service. This is the small config-wrapper build V1 runs
(`ordo-ai-stack-model-gateway:latest`) — a pinned LiteLLM base plus the stack's config: the
canonical **`local-chat`** alias, the `local-embed` alias, the throughput callback, and the
entrypoint that templates the config placeholders at startup.

## Model list (what `/v1/models` and `/model/info` advertise)

| id | mode | backend | notes |
|----|------|---------|-------|
| `local-chat` | chat | `llamacpp` (GPU) | Canonical auto-routing alias; fails over to the CPU pin on GPU eviction. |
| *\<gpu model\>* | chat | `llamacpp` (GPU) | Explicit pin of the GPU deployment — no failover. Name derived from `LLAMACPP_MODEL` (basename, lowercased; e.g. `qwen3.8-27b-q6_k`). |
| *\<cpu model\>*`-cpu` | chat | `llamacpp-cpu` | Always-on CPU fallback (opt-in `cpu-fallback` profile). Name derived from `LLAMACPP_CPU_MODEL`. |
| `local-embed` | embedding | `llamacpp-embed` | nomic-embed-text-v1.5, 768-dim, ctx 8192. |

The two pin-alias names are **derived at startup from the deployed GGUF filenames** — a
model swap renames them automatically, so nothing version-named is hardcoded in the config.
Only `local-chat` and `local-embed` are stable ids; anything that must survive a model swap
should use those.

Every entry carries a fully populated `model_info` block (mode, context window, output cap,
capability flags, description, backing GGUF) — that block IS the gateway's model
documentation for clients. Deployment-variable values are placeholder-templated by the
entrypoint from the same `.env` the backend llama-server containers read, so the advertised
metadata tracks the running deployment instead of drifting:

| placeholder | env var (default) |
|-------------|-------------------|
| `__MASTER_KEY__` | `LITELLM_MASTER_KEY` (required) |
| `__CTX_SIZE__` | `LLAMACPP_CTX_SIZE` (262144) |
| `__N_PREDICT__` | `LLAMACPP_N_PREDICT` (65536) |
| `__CPU_CTX_SIZE__` | `LLAMACPP_CPU_CTX` (131072) |
| `__GPU_WEIGHTS__` | `LLAMACPP_MODEL` (model.gguf) |
| `__CPU_WEIGHTS__` | `LLAMACPP_CPU_MODEL` (Qwen3.6-35B-A3B-UD-Q4_K_M.gguf) |
| `__EMBED_WEIGHTS__` | `LLAMACPP_EMBED_MODEL` (nomic-embed-text-v1.5.Q4_K_M.gguf) |
| `__GPU_IMAGE__` | `LLAMACPP_IMAGE` (llama.cpp) |
| `__GPU_MODEL_NAME__` | derived: `LLAMACPP_MODEL` basename, lowercased, `.gguf` stripped |
| `__CPU_MODEL_NAME__` | derived: `LLAMACPP_CPU_MODEL` basename, lowercased, + `-cpu` |
| `__GPU_SUPPORTS_VISION__` | derived: `true` iff `LLAMACPP_MMPROJ` is non-empty |

`supports_vision` is derived from whether the GPU server actually loads an mmproj. The
remaining `supports_*` flags (tools, reasoning) describe the llama-server invocation
(`--jinja`, `--reasoning-format`) rather than a model family, and are static in the config.

The Ordo stack references it as a **project buildable image** (`ordo/model-gateway:latest`) — pinned by
its build context, not pulled from a registry — so `ordo preflight` reports a missing one as
"build first", never "Docker will pull". This is why the Ordo stack does NOT reference the unconfigured
upstream `ghcr.io/berriai/litellm:main` directly: that image has no `local-chat` alias.

## Build
```
docker build -t ordo/model-gateway:latest services/model-gateway
```

## Files
- `Dockerfile` — pins `ghcr.io/berriai/litellm:v1.82.3@sha256:ac95e49049e0bb5f2c5a2b0f0452e5d968844b8196cf6efbd2e77d6ef862f7e5` (the running version; bump deliberately to a specific vX.Y.Z + digest), installs the config + callback.
- `litellm_config.yaml` — the model list + per-model `model_info` documentation (no secrets;
  all `__*__` placeholders are entrypoint-substituted at runtime).
- `entrypoint.sh` — renders the template with the master key + deployment metadata from `.env`.
- `throughput_callback.py` — posts per-completion tok/s + TTFT samples to the dashboard.

`LITELLM_MASTER_KEY` and `THROUGHPUT_RECORD_TOKEN` are supplied at runtime from the
operator-managed `secrets.env` (never baked).
