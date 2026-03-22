# AI-toolkit

Hey, I am Cam, I made this repo to simplify my local-LLM setup. I wanted a bunch of tools setup in a single spot -- and of course, all dockerized. 

Ollama + Open WebUI + ComfyUI + N8N in Docker. One command (`./compose up -d`), auto-detects hardware for best performance.

→ [Getting started](docs/GETTING_STARTED.md) · [Troubleshooting](docs/runbooks/TROUBLESHOOTING.md) · [Architecture](docs/Product%20Requirements%20Document.md)

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

## First-time setup

1. **Clone** the repo to your target drive (e.g. `F:\AI-toolkit` or `~/AI-toolkit`).
2. **Create `.env`** — copy `.env.example` to `.env` and set `BASE_PATH` to your install path.
3. **Create directories** — run the setup script (also auto-detects GPU and configures compute, and seeds ComfyUI MCP workflows from `workflow-templates/comfyui-workflows/` into `data/comfyui-workflows/` when missing):
   - **Windows (PowerShell):** `.\scripts\ensure_dirs.ps1`
   - **Linux/Mac:** `./scripts/ensure_dirs.sh`
4. **Start services:** `.\compose.ps1 up -d` (Windows) or `./compose up -d` (Linux/Mac)
5. **Open the dashboard** at [localhost:8080](http://localhost:8080).
6. **Pull models** — use the "Starter pack" button or select models from the dropdown.
7. **Chat** — open [localhost:3000](http://localhost:3000) (Open WebUI).

**No GPU?** Start only the core stack: `.\compose.ps1 up -d ollama dashboard open-webui` — see [Troubleshooting](docs/runbooks/TROUBLESHOOTING.md).

## Daily use

One command — auto-detects hardware and starts with best settings:

```powershell
.\compose.ps1 up -d      # Windows
```

```bash
./compose up -d          # Linux/Mac
```

## Quick start (copy-paste)

**Windows (PowerShell):**
```powershell
cd F:\AI-toolkit
copy .env.example .env
.\scripts\ensure_dirs.ps1
.\compose.ps1 up -d
```

**Linux/Mac:**
```bash
cd ~/AI-toolkit
cp .env.example .env
./scripts/ensure_dirs.sh
./compose up -d
```

All services start by default. Open the **dashboard** at [localhost:8080](http://localhost:8080) to manage models and see service status.

**If ComfyUI or OpenClaw fail:** The dashboard shows troubleshooting hints. ComfyUI uses auto-detected compute (NVIDIA/AMD/Intel/CPU); OpenClaw needs `OPENCLAW_GATEWAY_TOKEN` in the project root `.env` (ensure_dirs adds it if missing). See [Troubleshooting](docs/runbooks/TROUBLESHOOTING.md).

**On-demand commands** (run when you want to pull models):
- `.\compose.ps1 run --rm model-puller` / `./compose run --rm model-puller` — pull Ollama models from `.env`
- `.\compose.ps1 run --rm comfyui-model-puller` — download LTX-2 models (~60 GB)
- `.\compose.ps1 run --rm openclaw-cli onboard` — OpenClaw setup

## Dashboard

The **dashboard** at [localhost:8080](http://localhost:8080) gives you a single web UI to:

- **View all models** — Ollama (LLM) and ComfyUI (LTX-2) in one place
- **Restart services** — when `OPS_CONTROLLER_TOKEN` is set in `.env`
- **Pull models** — searchable dropdown with 150+ Ollama models; or type any model name
- **Jump to services** — Open WebUI, ComfyUI, N8N, OpenClaw, MCP Gateway

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

All data is stored under `BASE_PATH` via bind mounts — no Docker named volumes.

| Path | Contents |
|------|----------|
| `data/ollama` | Ollama models |
| `data/open-webui` | Users, chats, settings |
| `data/comfyui-output` | Generated images/video |
| `data/comfyui-workflows` | API-format workflows for MCP (`list_workflows` / `run_workflow`), OpenClaw (`/comfyui-workflows`), and ComfyUI UI sidebar under **Workflow → mcp-api** |
| `data/n8n-data` | Workflows |
| `data/n8n-files` | Shared files |
| `data/openclaw` | OpenClaw config + workspace (SOUL.md, AGENTS.md, TOOLS.md) |
| `models/comfyui/` | LTX-2 models (downloaded on demand) |

## MCP (Model Context Protocol)

The [MCP Gateway](mcp/) exposes shared MCP tools (web search, GitHub, etc.) to all services. Add servers via `MCP_GATEWAY_SERVERS` in `.env`. Connect Open WebUI, N8N, Cursor, and OpenClaw to `http://localhost:8811/mcp`. See [mcp/README.md](mcp/README.md).

## OpenClaw

[OpenClaw](openclaw/) is a personal AI assistant, integrated in the main compose. See [openclaw/README.md](openclaw/README.md) for token setup.

## Commands

```powershell
# Project management CLI
./ai-toolkit initialize   # Bootstrap directories, generate tokens, detect hardware, start services
./ai-toolkit help

# Docker compose wrapper
.\compose.ps1 up -d       # Start all services (auto-detects hardware)
.\compose.ps1 logs -f ollama
.\compose.ps1 down        # Stop
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
