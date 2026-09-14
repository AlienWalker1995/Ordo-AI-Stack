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

Every local model's `input_cost_per_token` / `output_cost_per_token` (in both `litellm_params`,
what LiteLLM actually bills against, and `model_info`, what `/model/info` and the UI display) is
electricity-derived from `ordo.yaml`'s `cost:` block (rig power draw, the operator's $/kWh rate
and measured tokens/sec) rather than hardcoded. `local-embed` only carries an input cost
(embedding has no output tokens). Leave `cost:` unset and every model prices at $0/token, same
as before this existed.

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
| `__LOCAL_INPUT_COST_PER_TOKEN__` | `LOCAL_INPUT_COST_PER_TOKEN` (0) |
| `__LOCAL_OUTPUT_COST_PER_TOKEN__` | `LOCAL_OUTPUT_COST_PER_TOKEN` (0) |

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

## Admin UI
LiteLLM's admin UI (keys, teams, spend, MCP servers, models) is at `https://llm.<tailnet>/ui/`
(or `https://<host>:8449/ui/` when the tailnet-names sidecars are disabled), behind the Google
SSO gate like every other UI in the stack. Once past SSO, log in to LiteLLM itself with
username `admin` and password = `LITELLM_MASTER_KEY` (from `out/secrets.env`; never write the
key value anywhere). The gateway sets `FORWARDED_ALLOW_IPS=*` so uvicorn trusts the
`X-Forwarded-Proto`/`X-Forwarded-Host` headers Caddy adds from its project-network address;
without it, the post-SSO redirects come back `http://` on a TLS-only port and the login fails.

## Files
- `Dockerfile` — pins `ghcr.io/berriai/litellm:v1.100.1@sha256:a3715fa7ad8387941ab697259bd2881d68931657247a41984f90fae6d11c62bf` (bump deliberately to a specific vX.Y.Z + digest), installs the config + helpers.
- `litellm_config.yaml` — the model list + per-model `model_info` documentation (no secrets;
  all `__*__` placeholders are entrypoint-substituted at runtime; the master key is read
  straight from the environment via `os.environ/LITELLM_MASTER_KEY`).
- `entrypoint.sh` — refuses a missing or weak `LITELLM_MASTER_KEY` (shape `sk-<32+ chars>`),
  renders the template with the deployment metadata from `.env`, then merges the rendered
  MCP server fragment (`/config/mcp_servers.yaml`) into the config.
- `merge_mcp_config.py` — folds the render-emitted `mcp_servers:` fragment into the rendered
  config; exits 2 when the fragment is missing or malformed (never boots an empty tool set).
- `throughput_callback.py` — posts per-completion tok/s + TTFT samples to the dashboard.
- `bootstrap_keys.py`: idempotent LiteLLM virtual-key provisioning from the rendered
  `out/model-gateway/keys.json` (runs as the `model-gateway-keys` one-shot).

`LITELLM_MASTER_KEY` and `THROUGHPUT_RECORD_TOKEN` are supplied at runtime from the
operator-managed `secrets.env` (never baked).
