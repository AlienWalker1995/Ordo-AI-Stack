# Configuration Quick Reference

Short reference for the env vars, MCP config, and compute overrides you'll touch most often. `.env.example` is the canonical list; this page highlights the common ones and how they interact.

## Environment Variables

Copy `.env.example` to `.env` and set at least `BASE_PATH`. Everything else has sensible defaults.

### Required

| Variable | Default | Purpose |
|---|---|---|
| `BASE_PATH` | `.` | Repository root (forward slashes on Windows, e.g. `C:/dev/ordo-ai-stack`) |

### Commonly set

| Variable | Default | Purpose |
|---|---|---|
| `DATA_PATH` | `${BASE_PATH}/data` | Override data directory location |
| `DEFAULT_MODEL` | `local-chat` | Canonical model alias used by Open WebUI, Hermes, and LiteLLM |
| `GGUF_MODELS` | *(see `.env.example`)* | Hugging Face repo(s) of GGUF files to pull for llama.cpp (`docker compose --profile models run --rm gguf-puller`) |
| `OPS_CONTROLLER_TOKEN` | *(empty)* | Required for dashboard-driven service lifecycle (`openssl rand -hex 32`) |
| `DASHBOARD_AUTH_TOKEN` | *(empty)* | Optional Bearer auth on dashboard `/api/*` |
| `HF_TOKEN` | *(empty)* | Hugging Face token for gated model downloads |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | *(empty)* | GitHub MCP server token; also passed to `comfyui` as `GITHUB_TOKEN` for Manager API |
| `COMPUTE_MODE` | *(auto-detected)* | Override GPU type: `nvidia`, `amd`, `intel`, `cpu` |

### Hermes Agent

See [hermes-agent.md](hermes-agent.md) for the full setup flow.

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_DASHBOARD_PORT` | `9119` | Host port for the Hermes dashboard |
| `DISCORD_BOT_TOKEN` | *(empty)* | Discord bot token. Managed via SOPS (`secrets/discord_token.sops`) or `DISCORD_BOT_TOKEN_FILE=/run/secrets/discord_token`; inline `DISCORD_TOKEN=` in `.env` is no longer accepted. |
| `DISCORD_ALLOWED_USERS` | *(empty)* | Comma-separated Discord user IDs authorized to DM / invoke the bot. Required for Discord use. |
| `DISCORD_ALLOWED_CHANNELS` | *(empty)* | Comma-separated channel IDs where the bot may respond. Optional. |
| `DISCORD_REQUIRE_MENTION` | `true` | Require `@bot` mention to respond. |

### RAG (`--profile rag`)

| Variable | Default | Purpose |
|---|---|---|
| `EMBED_MODEL` | `nomic-embed-text-v1.5.Q4_K_M.gguf` | Embedding model used by `rag-ingestion` and Open WebUI |
| `RAG_COLLECTION` | `documents` | Qdrant collection (must match Open WebUI / ingestion) |
| `RAG_CHUNK_SIZE` | `400` | Chunk size in tokens |
| `RAG_CHUNK_OVERLAP` | `50` | Chunk overlap in tokens |
| `QDRANT_PORT` | `6333` | Qdrant host port (change if something else already uses 6333) |

### Voice STT/TTS (`--profile voice`)

Opt-in local speech services with OpenAI-compatible APIs. Both services run on
the **secondary GPU** by default (the smallest GPU, e.g. GTX 1070) to leave the
primary GPU free for the LLM. On single-GPU hosts they share the primary.
`detect_hardware.py` seeds the GPU pin into `overrides/gpu-assignments.yml`; the
ops-controller model registry (`voice-stt` / `voice-tts` records) owns the intent.

**Enable:**

```bash
docker compose --profile voice up -d
```

| Variable | Default | Purpose |
|---|---|---|
| `STT_MODEL` | `Systran/faster-whisper-small` | Hugging Face repo ID for faster-whisper |
| `STT_COMPUTE_TYPE` | `int8` | Quantization type (`int8` is Pascal-compatible; use `float16` on Turing+) |
| `TTS_VOICE` | `af_bella` | Default voice label (registry record + client default). Kokoro selects the voice **per request** via the API `voice` param — this is not a container env. |

**Internal endpoints (backend network only — no host ports):**

| Service | URL | API |
|---|---|---|
| STT | `http://stt:8000/v1` | OpenAI-compatible `/v1/audio/transcriptions` |
| TTS | `http://tts:8880/v1` | OpenAI-compatible `/v1/audio/speech` |

**Hermes wiring (Discord voice memos):**

- **STT (voice memo → text): fully local.** Hermes' STT openai provider takes its
  base URL from the `STT_OPENAI_BASE_URL` env, which `docker-compose.yml` sets on
  `hermes-gateway` to `http://stt:8000/v1`. Set in `data/hermes/config.yaml`:
  `stt.provider: openai`, `stt.openai.model: Systran/faster-whisper-small`,
  `stt.openai.api_key: local`, `stt.enabled: true`. Inbound Discord voice messages
  are then auto-transcribed on the secondary GPU.
- **TTS (voice reply): edge by default; local Kokoro available but not yet Hermes-wired.**
  Hermes auto-replies in voice when the input was voice. Its default TTS provider is
  `edge` (Microsoft cloud, free, works out of the box). Pointing Hermes' *openai* TTS
  provider at the local Kokoro service requires `tts.openai.base_url`, but the current
  Hermes config schema does **not** persist a TTS `base_url` (and there is no env for
  it), so this Hermes version cannot target local Kokoro for replies. The Kokoro
  service is still deployed + registry-managed and reachable at `http://tts:8880/v1`
  for n8n / the reel pipeline / scripts / a future Hermes that honours a TTS base URL.
  For a fully-local reply voice today, use Hermes' native `neutts` provider (on-device).

**STT** weights download once to `${DATA_PATH}/voice/hf-cache` (persists across
recreates). **TTS** (Kokoro) bakes its models into the image — no runtime download,
no volume needed.

## TurboQuant KV-Cache (llama.cpp)

The `llamacpp` service runs a custom build from the [AmesianX/TurboQuant](https://github.com/AmesianX/TurboQuant) fork, produced by `llamacpp/Dockerfile` and pinned to a specific commit. On top of mainline's KV-cache quant types (`q4_0`, `q8_0`, etc.) it adds a family of TurboQuant types named `tbq*` and `tbqp*` that use Walsh–Hadamard rotation + Lloyd–Max scalar quantization, optionally with a 1-bit QJL residual (the `tbqp*` packed variants).

### Which type should I pick?

| Type | Approach | Effective bpw | Quality |
|---|---|---|---|
| `tbqp3_0` | 2-bit Lloyd-Max + 1-bit QJL residual | ~2.5 bpw | Marginal dip — best compression, paper's two-stage variant |
| `tbq3_0` | 3-bit Lloyd-Max after WHT rotation | ~3 bpw | Near-neutral |
| `tbqp4_0` | 3-bit Lloyd-Max + 1-bit QJL residual | ~3.5 bpw | Very close to fp16 |
| `tbq4_0` | 4-bit Lloyd-Max after WHT rotation | ~4 bpw | Closest to fp16 in the tbq family |
| `q4_0` | Mainline block-scaled 4-bit | ~4.5 bpw | Small ppl loss |

Suffix variants (`_1`, `_2`, `_3`) are head-dim specialized: `_1` for head_dim=128, `_2` for head_dim=64, `_3` for double WHT per-head. Use `_0` unless benchmarking shows a head-dim-specific variant helps your model.

### Enabling it

Set in `.env`:

```
LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION=1
LLAMACPP_KV_CACHE_TYPE_K=tbqp3_0   # or tbq3_0 / tbqp4_0 / tbq4_0
LLAMACPP_KV_CACHE_TYPE_V=tbqp3_0   # matching K and V is recommended
```

Then `docker compose build llamacpp && docker compose up -d llamacpp`. First build takes ~25–35 min (compiles CUDA kernels for Blackwell sm_120); subsequent builds reuse the buildx layer cache.

### Non-negotiable: Flash Attention

TurboQuant kernels silently corrupt output without Flash Attention. The shell wrapper at `scripts/llamacpp/run-llama-server.sh` appends `--flash-attn on` automatically whenever `LLAMACPP_KV_CACHE_TYPE_K` or `LLAMACPP_KV_CACHE_TYPE_V` contains `tbq`, overriding any `LLAMACPP_FLASH_ATTN=auto|off`. Do not try to disable this.

### VRAM sizing cheat sheet

Single-GPU budget = VRAM − driver overhead (~1.5 GB) − weights − compute buffer (~1.5 GB). Divide by per-token KV size for max on-GPU context.

Example on 32 GB 5090 with a 19 GB Q4_K_M 31B model (10 GB KV budget):

| KV type | Per-token KV | Max context fully on GPU |
|---|---|---|
| fp16 | ~533 KiB | ~20k |
| q8_0 | ~283 KiB | ~38k |
| q4_0 | ~150 KiB | ~72k |
| tbq4_0 | ~130 KiB | ~80k |
| tbq3_0 / tbqp4_0 | ~100 KiB | ~105k |
| tbqp3_0 | ~83 KiB | ~128k |

### Rollback to upstream

In `.env`:
```
LLAMACPP_IMAGE=ghcr.io/ggml-org/llama.cpp:server-cuda
LLAMACPP_KV_CACHE_TYPE_K=q4_0
LLAMACPP_KV_CACHE_TYPE_V=q4_0
```
`docker compose up -d llamacpp`. No code changes required. Upstream does not understand `tbq*` types and will reject them at startup.

## Model Registry

The model registry is the single source of truth for which model runs on which GPU. It is backed by `data/model-registry.json` and managed via:

- **Dashboard** — Models view (swap active model, set VRAM estimate) and GPU view (reassign GPU pin per model).
- **Hermes verbs** — `list_models`, `gpu_status`, `set_active_model`, `assign_model_gpu`, `register_model` (see [mcp/README.md](../mcp/README.md)).
- **ops-controller REST API** — `/registry/*` endpoints (auth required):

| Endpoint | Method | Purpose |
|---|---|---|
| `/registry/models` | GET | List all registered models |
| `/registry/models/{id}` | GET | Read one model record |
| `/registry/models` | POST | Define / upsert a model record |
| `/registry/models/{id}` | DELETE | Remove a model record |
| `/registry/models/{id}/assign-gpu` | POST | Pin a model to a specific GPU UUID |
| `/registry/models/{id}/enable` | POST | Swap active model (writes .env, recreates service) |
| `/registry/gpus` | GET | Live GPU inventory with per-model VRAM breakdown |

On startup the ops-controller reconciles the registry from `.env` (LLAMACPP_MODEL, LLAMACPP_EMBED_MODEL, etc.) and `overrides/gpu-assignments.yml`. Reconcile is **seed-only**: records that already exist are never overwritten. Operators change models via the registry verbs; `.env` and `gpu-assignments.yml` are derived artifacts.

The registry path can be overridden with `MODEL_REGISTRY_PATH` (default `/data/model-registry.json`).

## MCP Server Configuration

Repo templates live under `mcp/gateway/`; runtime files are in `data/mcp/` (bind-mounted into the gateway). See [mcp/README.md](../mcp/README.md).

Enabled servers are listed in `data/mcp/servers.txt` (one per line). Metadata, per-server `allow_clients`, and rate limits live in `data/mcp/registry.json`.

Default servers: `duckduckgo`, `n8n`, `searxng`, `comfyui`, `orchestration`, `playwright` (the `searxng` server proxies the self-hosted SearXNG instance — no external API key; `playwright` is stack-pinned headless-Chromium browser automation). Override with `MCP_GATEWAY_SERVERS` in `.env`:

```
MCP_GATEWAY_SERVERS=duckduckgo,github-official
```

Edits to `servers.txt` trigger a gateway reload within ~10 seconds — no container restart needed.

## Compute Configuration

`scripts/detect_hardware.py` runs via the `compose` wrapper and writes `overrides/compute.yml` (gitignored). It's re-detected every time you invoke `./compose`.

To override manually, set `COMPUTE_MODE` and `COMPOSE_FILE` in `.env`:

```
COMPUTE_MODE=nvidia
COMPOSE_FILE=docker-compose.yml;overrides/compute.yml
```

**ComfyUI `CLI_ARGS`:** Set `COMFYUI_CLI_ARGS` in `.env`, or accept the default that `detect_hardware.py` supplies (GPU stacks get `--normalvram` so text encoders stay on GPU). Without the var, the compose base default is `--cpu --enable-manager`.

**ComfyUI custom-node deps:** The `comfyui` container's startup wrapper auto-installs every `requirements.txt` it finds under `/root/ComfyUI/custom_nodes/*/` before launching ComfyUI. Idempotent (pip skips already-satisfied specifiers). Adding a new custom node? Drop it under `data/comfyui-storage/ComfyUI/custom_nodes/` and `docker compose up -d --force-recreate comfyui` — its deps land automatically. Failures on individual files (e.g. malformed pin) are logged as `[deps] WARN failed` and don't block startup.

## Data Persistence Rules

All `data/` and `models/` directories are bind-mounted and persist across container restarts.

| Directory | Purpose |
|---|---|
| `data/hermes/` | Hermes agent runtime state (sessions, per-user allowlists) |
| `data/qdrant/` | Qdrant vector DB storage |
| `data/rag-input/` | Drop files here for `rag-ingestion` |
| `data/ops-controller/` | Audit logs |
| `data/mcp/` | `servers.txt`, `registry.json`, `registry-custom.yaml` |
| `data/dashboard/` | Dashboard throughput / benchmark data |
| `data/comfyui-storage/` | ComfyUI outputs, custom nodes, local configs |
| `models/gguf/` | llama.cpp GGUF files |
| `models/comfyui/` | ComfyUI checkpoints, LoRAs, VAEs, encoders |

`/tmp` inside containers is tmpfs; nothing there survives a restart.

## Network Ports

| Service | Host port | Description |
|---|---|---|
| Dashboard | `8080` | Dashboard API + control center |
| Open WebUI | `3000` | Chat interface |
| Model Gateway | `11435` | OpenAI-compatible model endpoint (LiteLLM in front of llama.cpp) |
| ComfyUI | `8188` | Image / audio / video generation |
| n8n | `5678` | Workflow automation |
| Hermes dashboard | `9119` | Overridable via `HERMES_DASHBOARD_PORT` |
| MCP Gateway | `8811` | Published on host so external clients (Cursor, Claude Desktop) can reach it |
| Qdrant | `6333` | RAG profile only |
| Ops Controller | internal `9000` | Not published on the host |

## Audit Log Schema

`data/ops-controller/audit.log` is JSONL, append-only, one event per line:

```json
{"timestamp":"2026-03-22T10:00:00Z","action":"model_pulled","model":"qwen3:8b","status":"success"}
{"timestamp":"2026-03-22T10:01:00Z","action":"service_started","service":"llamacpp","status":"success"}
```

## Minimal `.env`

```
BASE_PATH=.

# Models
MODELS=qwen3:8b,deepseek-r1:7b,nomic-embed-text
DEFAULT_MODEL=local-chat

# Ops
OPS_CONTROLLER_TOKEN=ops-controller-token-here
DASHBOARD_AUTH_TOKEN=dashboard-token-here

# Optional
HF_TOKEN=
GITHUB_PERSONAL_ACCESS_TOKEN=

# RAG
EMBED_MODEL=nomic-embed-text-v1.5.Q4_K_M.gguf
```
