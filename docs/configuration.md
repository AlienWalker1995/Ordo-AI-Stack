# Configuration Quick Reference

Short reference for the env vars, MCP config, and compute overrides you'll touch most often. `.env.example` is the canonical list; this page highlights the common ones and how they interact.

## Environment Variables

Copy `.env.example` to `.env` and set at least `BASE_PATH`. Everything else has sensible defaults.

### Required

| Variable | Default | Purpose |
|---|---|---|
| `BASE_PATH` | `.` | Repository root (forward slashes on Windows, e.g. `C:/dev/AI-toolkit`) |

### Commonly set

| Variable | Default | Purpose |
|---|---|---|
| `DATA_PATH` | `${BASE_PATH}/data` | Override data directory location |
| `DEFAULT_MODEL` | `local-chat` | Canonical model alias used by Open WebUI, Hermes, and LiteLLM |
| `MODELS` | *(see `.env.example`)* | Comma-separated Ollama models to pull on first start |
| `OPS_CONTROLLER_TOKEN` | *(empty)* | Required for dashboard-driven service lifecycle (`openssl rand -hex 32`) |
| `DASHBOARD_AUTH_TOKEN` | *(empty)* | Optional Bearer auth on dashboard `/api/*` |
| `HF_TOKEN` | *(empty)* | Hugging Face token for gated model downloads |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | *(empty)* | GitHub MCP server token; also passed to `comfyui` as `GITHUB_TOKEN` for Manager API |
| `TAVILY_API_KEY` | *(empty)* | Required if the `tavily` MCP server is enabled |
| `COMPUTE_MODE` | *(auto-detected)* | Override GPU type: `nvidia`, `amd`, `intel`, `cpu` |

### Hermes Agent

See [hermes-agent.md](hermes-agent.md) for the full setup flow.

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_DASHBOARD_PORT` | `9119` | Host port for the Hermes dashboard |
| `DISCORD_BOT_TOKEN` | *(empty)* | Discord bot token. Legacy `DISCORD_TOKEN` is aliased automatically. |
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

## MCP Server Configuration

Repo templates live under `mcp/gateway/`; runtime files are in `data/mcp/` (bind-mounted into the gateway). See [mcp/README.md](../mcp/README.md).

Enabled servers are listed in `data/mcp/servers.txt` (one per line). Metadata, per-server `allow_clients`, and rate limits live in `data/mcp/registry.json`.

Default servers: `duckduckgo`, `n8n`, `tavily`, `comfyui` (Tavily requires `TAVILY_API_KEY`). Override with `MCP_GATEWAY_SERVERS` in `.env`:

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
| `models/ollama/` | Ollama model blobs |
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
| Ollama | `11434` | **Backend-only by default.** Expose via `overrides/ollama-expose.yml` |
| Qdrant | `6333` | RAG profile only |
| Ops Controller | internal `9000` | Not published on the host |

## Audit Log Schema

`data/ops-controller/audit.log` is JSONL, append-only, one event per line:

```json
{"timestamp":"2026-03-22T10:00:00Z","action":"model_pulled","model":"qwen3:8b","status":"success"}
{"timestamp":"2026-03-22T10:01:00Z","action":"service_started","service":"ollama","status":"success"}
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
