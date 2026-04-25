# Configuration Quick Reference

Short reference for the env vars, MCP config, and compute overrides you'll touch most often. `.env.example` (plaintext) and `secrets/.env.sops` (encrypted) are the canonical sources; this page highlights the common ones and how they interact.

## Environment Variables

Plaintext settings live in `.env` (copy from `.env.example`); managed tokens live in `secrets/.env.sops` and the file-form `secrets/*.sops` blobs (decrypted into `~/.ai-toolkit/runtime/` by `make decrypt-secrets`).

### Required

| Variable | Default | Purpose |
|---|---|---|
| `BASE_PATH` | `.` | Repository root (forward slashes on Windows, e.g. `C:/dev/AI-toolkit`) |

### Commonly set (plaintext `.env`)

| Variable | Default | Purpose |
|---|---|---|
| `DATA_PATH` | `${BASE_PATH}/data` | Override data directory location |
| `LLAMACPP_MODEL` | `Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf` | GGUF filename under `models/gguf/` |
| `DEFAULT_MODEL` | `local-chat` | Canonical model alias used by Open WebUI, Hermes, and LiteLLM |
| `EMBED_MODEL` | `nomic-embed-text-v1.5.Q4_K_M.gguf` | GGUF embedding model served by `llamacpp-embed` |
| `COMPUTE_MODE` | *(auto-detected)* | Override GPU type: `nvidia`, `amd`, `intel`, `cpu` |
| `COMPOSE_FILE` | *(auto-set)* | Compose file chain. Linux/macOS: `docker-compose.yml:overrides/compute.yml`. Windows: `docker-compose.yml;overrides/compute.yml`. |
| `CADDY_TAILNET_HOSTNAME` | *(unset)* | e.g. `ordo.<tailnet>.ts.net` — used by Caddy for the SSO host block |
| `CADDY_TAILNET_DOMAIN` | *(unset)* | e.g. `<tailnet>.ts.net` — cookie domain for oauth2-proxy |
| `CADDY_BIND` | *(no default; required)* | Tailnet IP from `tailscale ip -4`; the compose `:?` failsafe refuses to start with an empty value |

### Managed tokens (`secrets/.env.sops`)

Edit with `sops secrets/.env.sops`. After save, `make decrypt-secrets` and restart the dependent service. Full rotation guide: [docs/runbooks/secrets.md](runbooks/secrets.md).

| Variable | Purpose |
|---|---|
| `LITELLM_MASTER_KEY` | Bearer presented by clients to model-gateway |
| `DASHBOARD_AUTH_TOKEN` | Bearer fallback when Caddy's SSO isn't in the path |
| `OPS_CONTROLLER_TOKEN` | Bearer for any caller of ops-controller's HTTP API |
| `OAUTH2_PROXY_CLIENT_ID` | Google OAuth Web client ID |
| `OAUTH2_PROXY_CLIENT_SECRET` | Google OAuth Web client secret |
| `OAUTH2_PROXY_COOKIE_SECRET` | 16/24/32 raw bytes; rotates browser sessions when changed |

### High-value tokens (Docker secret files)

Each lives in its own `secrets/<name>.sops` blob and is decrypted to `~/.ai-toolkit/runtime/secrets/<name>`, then mounted into containers at `/run/secrets/<name>`. The `_FILE` env-var bridge (`hermes/entrypoint.sh`, `mcp-gateway`'s entrypoint) reads the file into the env var the SDK expects.

| Secret file | App env var | Consumer |
|---|---|---|
| `discord_token` | `DISCORD_BOT_TOKEN` | `hermes-gateway` |
| `github_pat` | `GITHUB_PERSONAL_ACCESS_TOKEN` | `mcp-gateway` (GitHub server) |
| `hf_token` | `HF_TOKEN` | `ops-controller` (model pulls), `comfyui` |
| `tavily_key` | `TAVILY_API_KEY` | `mcp-gateway` (Tavily server) |
| `civitai_token` | `CIVITAI_TOKEN` | `comfyui` (model pulls) |

### Hermes Agent

Full setup flow: [hermes-agent.md](hermes-agent.md).

| Variable | Default | Purpose |
|---|---|---|
| `HERMES_DASHBOARD_PORT` | `9119` | Host port for the Hermes dashboard |
| `HERMES_HOST_DEV_MOUNT` | `/c/dev` (Windows), `/workspace` elsewhere | Where the host's project root is mounted into the Hermes container — keeps sibling `docker compose` calls valid by mirroring the host path |
| `DISCORD_ALLOWED_USERS` | *(empty)* | Comma-separated Discord user IDs authorized to DM / invoke the bot |
| `DISCORD_ALLOWED_CHANNELS` | *(empty)* | Comma-separated channel IDs where the bot may respond |
| `DISCORD_REQUIRE_MENTION` | `true` | Require `@bot` mention to respond |

### RAG (`--profile rag`)

| Variable | Default | Purpose |
|---|---|---|
| `EMBED_MODEL` | `nomic-embed-text-v1.5.Q4_K_M.gguf` | Embedding model used by `rag-ingestion` and Open WebUI |
| `RAG_COLLECTION` | `documents` | Qdrant collection (must match Open WebUI / ingestion) |
| `RAG_CHUNK_SIZE` | `400` | Chunk size in tokens |
| `RAG_CHUNK_OVERLAP` | `50` | Chunk overlap in tokens |
| `QDRANT_PORT` | `6333` | Qdrant host port (change if 6333 is taken) |

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

Example on 32 GB 5090 with **Qwen3.6-35B-A3B Q4_K_M** (~20 GB on GPU; MoE — only 3B active per token, so the KV cache is also small per active head):

| KV type | Per-token KV | Max context fully on GPU |
|---|---|---|
| fp16 | ~533 KiB | ~18k |
| q8_0 | ~283 KiB | ~35k |
| q4_0 | ~150 KiB | ~66k |
| tbq4_0 | ~130 KiB | ~75k |
| tbq3_0 / tbqp4_0 | ~100 KiB | ~95k |
| tbqp3_0 | ~83 KiB | ~120k |

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

Default servers: `duckduckgo`, `n8n`, `tavily`, `comfyui` (Tavily requires its key in `secrets/tavily_key.sops`). Override with `MCP_GATEWAY_SERVERS` in `.env`:

```
MCP_GATEWAY_SERVERS=duckduckgo,github-official
```

Edits to `servers.txt` trigger a gateway reload within ~10 seconds — no container restart needed.

## Compute Configuration

`scripts/detect_hardware.py` runs via the `compose` wrapper and writes `overrides/compute.yml` (gitignored). It's re-detected every time you invoke `./compose`.

The generator inspects `nvidia-smi` (or AMD/Intel equivalents). When it sees a GPU, it adds the right `deploy.resources.reservations.devices` stanza to `llamacpp`, `llamacpp-embed`, `comfyui`, and a `utility` reservation to `dashboard` so it can call `nvidia-smi` for the throughput panel. **If you skip the wrapper and run raw `docker compose up`, you must regenerate the override yourself**: `python scripts/detect_hardware.py`. A missing devices block manifests as `libcuda.so.1: cannot open shared object file` in the `llamacpp` log.

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
| `data/hermes/` | Stale (pre-volume-migration); kept for archival, NOT mounted live. Live state is the named volume `ordo-ai-stack_hermes-data`. |
| `data/qdrant/` | Qdrant vector DB storage |
| `data/rag-input/` | Drop files here for `rag-ingestion` |
| `data/ops-controller/` | Audit log (`audit.jsonl` + `audit.1.jsonl`) |
| `data/mcp/` | `servers.txt`, `registry.json`, `registry-custom.yaml` |
| `data/dashboard/` | Dashboard throughput / benchmark data |
| `data/comfyui-storage/` | ComfyUI outputs, custom nodes, local configs |
| `models/gguf/` | llama.cpp GGUF files (chat + embed) |
| `models/comfyui/` | ComfyUI checkpoints, LoRAs, VAEs, encoders |

`/tmp` inside containers is tmpfs; nothing there survives a restart.

## Network Ports

All ports bind to `0.0.0.0` by default, so they're reachable from anywhere your host's network can reach. The expectation is that **Tailscale is the network gate** (see [Getting Started — Tailscale + SSO front door](GETTING_STARTED.md#tailscale--sso-front-door)).

| Service | Host port | Description |
|---|---|---|
| Caddy (HTTPS, SSO front door) | `443` (bound to `${CADDY_BIND}` only) | Routes `/dash/`, `/api/*`, `/favicon.svg` to dashboard after SSO |
| Dashboard (backend) | `8080` (no host publish unless you opt out of SSO) | Dashboard API + control center |
| Open WebUI | `3000` | Chat interface (direct on tailnet) |
| Model Gateway | `11435` | OpenAI-compatible model endpoint (LiteLLM in front of llama.cpp) |
| ComfyUI | `8188` | Image / audio / video generation |
| n8n | `5678` | Workflow automation |
| Hermes dashboard | `9119` | Overridable via `HERMES_DASHBOARD_PORT` |
| MCP Gateway | `8811` | Published on host so Cursor / Claude Desktop / etc. can reach it |
| Qdrant | `6333` | RAG profile only |
| Ops Controller | internal `9000` | Not published on the host — only reachable from the Docker network |

## Audit Log Schema

`data/ops-controller/audit.jsonl` is JSONL, append-only, one event per privileged ops-controller call:

```json
{"ts": 1745611200.123, "caller": "hermes", "action": "container.restart", "target": "open-webui", "result": "ok"}
{"ts": 1745611205.456, "caller": "dashboard", "action": "compose.up", "target": "open-webui", "result": "ok"}
```

| Field | Type | Description |
|---|---|---|
| `ts` | float (Unix epoch seconds) | Event timestamp |
| `caller` | string | Identity of the caller (`hermes`, `dashboard`, …) |
| `action` | string | `container.list`, `container.logs`, `container.restart`, `compose.up`, `compose.down`, `compose.restart` |
| `target` | string | Action-specific target (container name, service, or empty for whole-stack) |
| `result` | string | `ok` or short error message |

Rotation: `audit.jsonl` rolls to `audit.1.jsonl` at `AUDIT_LOG_MAX_BYTES` (default 50 MB). One historical generation; older data is dropped.

## Minimal `.env`

```
BASE_PATH=.

# Models
LLAMACPP_MODEL=Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf
DEFAULT_MODEL=local-chat
EMBED_MODEL=nomic-embed-text-v1.5.Q4_K_M.gguf

# SSO front door (set after `tailscale ip -4` and Google OAuth setup)
CADDY_TAILNET_HOSTNAME=ordo.<tailnet>.ts.net
CADDY_TAILNET_DOMAIN=<tailnet>.ts.net
CADDY_BIND=<tailnet-ip>

# TurboQuant KV (optional but recommended on Blackwell)
LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION=1
LLAMACPP_KV_CACHE_TYPE_K=tbqp3_0
LLAMACPP_KV_CACHE_TYPE_V=tbqp3_0
```

Tokens (`LITELLM_MASTER_KEY`, `DASHBOARD_AUTH_TOKEN`, `OPS_CONTROLLER_TOKEN`, `OAUTH2_PROXY_*`, all the high-value provider keys) live in `secrets/.env.sops` and `secrets/<name>.sops`, **not** in `.env`. See [secrets runbook](runbooks/secrets.md).
