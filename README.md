# AI-toolkit

Hey, I am Cam, I made this repo to simplify my local-LLM setup. I wanted a bunch of tools setup in a single spot -- and of course, all dockerized. 

Ollama + Open WebUI + ComfyUI + N8N in Docker. **Bootstrap + full stack:** `.\ai-toolkit.ps1 initialize` (Windows) or `./ai-toolkit initialize` (Linux/Mac). **Quick up:** `./compose up -d` — auto-detects hardware for best performance.

→ [Getting started](docs/GETTING_STARTED.md) · [Configuration](docs/configuration.md) · [Docker Runtime](docs/docker-runtime.md) · [Data](docs/data.md) · [Troubleshooting](docs/runbooks/TROUBLESHOOTING.md) · [Architecture](docs/Product%20Requirements%20Document.md)

## Services

| Service | Port | Purpose |
|---------|------|---------|
| **dashboard** | 8080 | **Unified model manager** — [localhost:8080](http://localhost:8080) |
| **ollama** | 11434 | Local LLM runtime (GPU) |
| **model-gateway** | 11435 | OpenAI-compatible proxy (Ollama/vLLM) |
| **open-webui** | 3000 | Chat UI — [localhost:3000](http://localhost:3000) |
| **comfyui** | 8188 | Stable Diffusion / LTX-2 — [localhost:8188](http://localhost:8188) |
| **n8n** | 5678 | Workflow automation — [localhost:5678](http://localhost:5678) |
| **OpenClaw** | **6680** (Control UI; add `?token=`) · 6682 = browser bridge only | [openclaw/](openclaw/) · optional [openclaw-secure](overrides/openclaw-secure.yml) uses **18789** on localhost |
| **MCP Gateway** | 8811 | Shared MCP tools — [mcp/](mcp/) |
| **ops-controller** | — | Start/stop/restart services from dashboard (set `OPS_CONTROLLER_TOKEN`) |
| model-puller | — | Ready to pull Ollama models on demand |
| comfyui-model-puller | — | Ready to download LTX-2 models (~60 GB) on demand |

## Setup

1. **Clone** to your drive (e.g. `F:\AI-toolkit` or `~/AI-toolkit`).
2. **Optional:** `copy .env.example .env` / `cp .env.example .env` and set `BASE_PATH` (and `DATA_PATH` if you want app data outside the repo). The initializer creates `.env` from the example if missing and sets `OPENCLAW_GATEWAY_TOKEN`.
3. **One command — full bootstrap + stack** (dirs, `ensure_dirs`, OpenClaw workspace seeds, GPU `compute.yml`, then `docker compose up -d --build --force-recreate`):

```powershell
cd F:\AI-toolkit
.\ai-toolkit.ps1 initialize
# cmd.exe: .\ai-toolkit.cmd initialize
```

```bash
cd ~/AI-toolkit
./ai-toolkit initialize
```

**Manual alternative** (no forced rebuild/recreate): `.\scripts\ensure_dirs.ps1` then `.\compose.ps1 up -d` (Windows) or `./scripts/ensure_dirs.sh` then `./compose up -d` (Linux/Mac).

4. Open the **dashboard** at [localhost:8080](http://localhost:8080); pull models from there; chat at [localhost:3000](http://localhost:3000).

**No GPU?** After init: `.\compose.ps1 up -d ollama dashboard open-webui` — see [Troubleshooting](docs/runbooks/TROUBLESHOOTING.md).

**If ComfyUI or OpenClaw fail:** See dashboard hints and [Troubleshooting](docs/runbooks/TROUBLESHOOTING.md).

## Daily use

**Full restart** (same as setup step 3): `.\ai-toolkit.ps1 initialize` or `./ai-toolkit initialize`.

**Quick bring-up** (no rebuild/recreate; `compose` runs hardware detection):

```powershell
.\compose.ps1 up -d
```

```bash
./compose up -d
```

**On-demand commands** (pull models when you want):
- `.\compose.ps1 run --rm model-puller` / `./compose run --rm model-puller` — pull Ollama models from `.env`
- `.\compose.ps1 run --rm comfyui-model-puller` — download LTX-2 models (~60 GB)
- `.\compose.ps1 run --rm openclaw-cli onboard` — OpenClaw setup

## Dashboard

The **dashboard** at [localhost:8080](http://localhost:8080) gives you a single web UI to:

- **View all models** — Ollama (LLM) and ComfyUI (LTX-2) in one place
- **Restart services** — when `OPS_CONTROLLER_TOKEN` is set in `.env`
- **ComfyUI custom-node Python deps** — authenticated `POST /api/comfyui/install-node-requirements` (proxied to ops-controller; OpenClaw can call this with `DASHBOARD_AUTH_TOKEN` — see `openclaw/workspace/agents/comfyui-assets.md`)
- **Pull models** — searchable dropdown with 150+ Ollama models; or type any model name
- **Jump to services** — Open WebUI, ComfyUI, N8N, OpenClaw, MCP Gateway
- **RAG** — `docker compose --profile rag up -d`, drop files in `data/rag-input/`; details in [Getting started — RAG](docs/GETTING_STARTED.md#rag-documents-in-chat)

**Not seeing updates?** After pulling code changes, rebuild: `.\compose.ps1 build dashboard` then `.\compose.ps1 up -d`

## Ollama models

Default models (set in `.env`):

- `deepseek-r1:7b` — reasoning
- `deepseek-coder:6.7b` — coding
- `nomic-embed-text` — embeddings / RAG

**Pull via dashboard** (recommended) or via CLI:

```bash
./compose run --rm model-puller   # on-demand from .env
# Or use the dashboard at localhost:8080
```

## ComfyUI (LTX-2)

ComfyUI starts independently. LTX-2 models (~60 GB) are downloaded on demand — first run takes a while; subsequent runs skip existing files.

**Includes:** LTX-2 checkpoint (fp8), LoRAs, latent upscaler, Gemma 3 12B text encoder.

**Pull via dashboard** (recommended) or:

```bash
./compose run --rm comfyui-model-puller
```

## Security

- **Open WebUI** — set `WEBUI_AUTH=True` in `.env` when exposing to a network.
- **OpenClaw** — requires `OPENCLAW_GATEWAY_TOKEN`. For Tailscale-only access, use `overrides/openclaw-secure.yml`. See [OPENCLAW_SECURE.md](openclaw/OPENCLAW_SECURE.md).
- **Ops Controller** — requires `OPS_CONTROLLER_TOKEN` for dashboard start/stop/restart.
- Never commit `.env`. Full threat model: [SECURITY.md](SECURITY.md).

## GPU / compute

**Auto-detection:** The setup script (`ensure_dirs`) runs `scripts/detect_hardware.py`, which detects your GPU and generates `overrides/compute.yml` (auto-generated, gitignored):

| Detected | Ollama | ComfyUI |
|----------|--------|---------|
| **NVIDIA** | GPU (NVIDIA Container Toolkit) | CUDA 12.8 |
| **AMD** | ROCm | ROCm |
| **Intel** | CPU | XPU |
| **CPU** | CPU | CPU (slower) |

The `compose` wrapper runs detection before every command, so `.\compose.ps1 up -d` or `./compose up -d` always uses the best settings.

**No GPU?** Run the minimal stack: `.\compose.ps1 up -d ollama dashboard open-webui`. ComfyUI will use CPU by default (slower). See [Troubleshooting](docs/runbooks/TROUBLESHOOTING.md).

## Architecture

```
User → Dashboard / Open WebUI / N8N / OpenClaw
         │
         ├── Model Gateway (:11435) → Ollama / vLLM
         ├── MCP Gateway (:8811) → shared tools
         └── Ops Controller (:9000) → Docker Compose lifecycle
```

Local-first, single model endpoint (OpenAI-compatible), dashboard never mounts docker.sock. Full design: [Product Requirements Document](docs/Product%20Requirements%20Document.md).

## Data

Bind mounts only (no Docker named volumes). Set **`BASE_PATH`** in `.env` to the repo root. Optional **`DATA_PATH`** defaults to `BASE_PATH/data`; many services use it, but some paths are still under `BASE_PATH/data` (see `docker-compose.yml`). Ollama model blobs live under **`models/ollama`**.

For detailed data schemas, lifecycle, and persistence rules, see **[docs/data.md](docs/data.md)**.

For core workspace layout and volume mounts, see **[docs/docker-runtime.md](docs/docker-runtime.md)**.

## MCP (Model Context Protocol)

The [MCP Gateway](mcp/) exposes shared MCP tools (web search, GitHub, etc.) to all services. Add servers via `MCP_GATEWAY_SERVERS` in `.env`. Connect Open WebUI, N8N, Cursor, and OpenClaw to `http://localhost:8811/mcp`. See [mcp/README.md](mcp/README.md).

## OpenClaw

[OpenClaw](openclaw/) is a personal AI assistant, integrated in the main compose. See [openclaw/README.md](openclaw/README.md) for token setup.

### OpenClaw Control UI Access

**Main Control UI:** `http://localhost:6680/?token=<OPENCLAW_GATEWAY_TOKEN>`

**Note:** The Control UI is at port `6680`. Port `6682` is the browser/CDP bridge only (used internally).

If the agent cannot write **`data/openclaw/workspace/MEMORY.md`** (`EACCES`), or **`TOOLS.md`** is still an old stub, run **`scripts/fix_openclaw_workspace_permissions.ps1`** (Windows) or **`./scripts/fix_openclaw_workspace_permissions.sh`** (Linux/Mac) from the repo root — it upgrades **`TOOLS.md`** from the template when needed, re-runs sync (**`chown`**), then restart **`openclaw-gateway`**. See [TROUBLESHOOTING — OpenClaw workspace](docs/runbooks/TROUBLESHOOTING.md#openclaw-workspace--eacces--permission-denied-on-memorymd-or-other-md).

### OpenClaw Core Workspace

The OpenClaw agent workspace lives at `data/openclaw/workspace/`. These files persist across container restarts:

| File | Description |
|---|-|
| `MEMORY.md` | **Persistent memory** — key file for agent continuity |
| `TOOLS.md` | Tool definitions and usage |
| `SOUL.md` | Core agent identity and purpose |
| `AGENTS.md` | Agent definitions |
| `USER.md` | User profile and preferences |

For complete workspace documentation, see **[docs/configuration.md](docs/configuration.md)** and **[docs/docker-runtime.md](docs/docker-runtime.md)**.

## Commands

```powershell
# One command: bootstrap + rebuild/recreate + start the full default stack (from repo root)
.\ai-toolkit.ps1 initialize
# cmd.exe: .\ai-toolkit.cmd initialize

# Docker compose wrapper (quick up — no forced rebuild/recreate)
.\compose.ps1 up -d
.\compose.ps1 logs -f ollama
.\compose.ps1 down
```

```bash
./ai-toolkit initialize
./ai-toolkit help
./compose up -d
```

## Tests and lint

Install test deps once (includes `pytest` and `ruff`):

```powershell
pip install -r tests/requirements.txt
python -m pytest tests/ -v
# Or: make test   (Linux/Mac)
python -m ruff check dashboard tests model-gateway ops-controller rag-ingestion scripts
# Or: make lint   (Linux/Mac)
```

CI runs **pytest**, **ruff**, and a **fixture-based** `validate_openclaw_config.py` check (see `.github/workflows/ci.yml`).

### Health checks (M7)

Use these when something fails to start or OpenClaw cannot reach models:

| What | Purpose |
|------|---------|
| **Doctor script** | Probes dashboard, model-gateway (`/health`, `/ready`), Ollama, MCP, then runs OpenClaw config validation. |
| **`GET /api/dependencies`** | Dashboard JSON: live probes for every entry in the dependency registry (same data as the **Dependencies** section in the UI). |
| **Model Gateway `GET /ready`** | L2 readiness (models listed); HTTP **503** if backends are down or no models are configured. |
| **`scripts/validate_openclaw_config.py`** | Ensures `openclaw.json` wires `models.providers.gateway` to the model-gateway OpenAI endpoint. |

```powershell
.\scripts\doctor.ps1
# Or: ./scripts/doctor.sh
# Optional: DOCTOR_DEPS_TIMEOUT_SEC — max seconds for GET /api/dependencies (default 120).
# Validate a specific file (optional --require in CI when the file must exist):
python scripts/validate_openclaw_config.py data/openclaw/openclaw.json
```

**Doctor output:** **WARN** on HTTP **404** for `/api/dependencies` or model-gateway `/ready` usually means the **container image is behind the repo** — run `docker compose build dashboard model-gateway` (or your compose wrapper) and recreate. **Ollama** (`localhost:11434`) and **MCP** (`localhost:8811`) are **WARN** by default: the main compose file keeps them **backend-only** (no host port). Use `overrides/ollama-expose.yml` and `overrides/mcp-expose.yml` if you want those URLs on localhost; set **`DOCTOR_STRICT=1`** to treat those probes as hard failures. The script reads `DASHBOARD_AUTH_TOKEN` from `.env` when probing the dashboard.

More copy-paste diagnostics (including `curl` for `/api/dependencies` and `/ready`): [TROUBLESHOOTING.md — Quick Diagnostics](docs/runbooks/TROUBLESHOOTING.md).

**Smoke test** (bring up services and verify health):

```powershell
.\scripts\smoke_test.ps1       # Windows
./scripts/smoke_test.sh         # Linux/Mac (or: make smoke-test)
```

## Runbooks

Operational runbooks in [docs/runbooks/](docs/runbooks/):

- [BACKUP_RESTORE.md](docs/runbooks/BACKUP_RESTORE.md) — Backup and restore data
- [UPGRADE.md](docs/runbooks/UPGRADE.md) — Upgrade images and config
- [TROUBLESHOOTING.md](docs/runbooks/TROUBLESHOOTING.md) — Common issues and fixes
