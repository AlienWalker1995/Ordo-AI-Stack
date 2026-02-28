# AI-toolkit

Hey, I am Cam, I made this repo to simplify my local-LLM setup. I wanted a bunch of tools setup in a single spot -- and of course, all dockerized. 

Ollama + Open WebUI + ComfyUI + N8N in Docker. One command (`./compose up -d`), auto-detects hardware for best performance.

→ [Repository structure](docs/STRUCTURE.md) · [Getting started](docs/GETTING_STARTED.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

## Services

| Service | Port | Purpose |
|---------|------|---------|
| **dashboard** | 8080 | **Unified model manager** — [localhost:8080](http://localhost:8080) |
| **ollama** | 11434 | Local LLM runtime (GPU) |
| **open-webui** | 3000 | Chat UI — [localhost:3000](http://localhost:3000) |
| **comfyui** | 8188 | Stable Diffusion / LTX-2 — [localhost:8188](http://localhost:8188) |
| **n8n** | 5678 | Workflow automation — [localhost:5678](http://localhost:5678) |
| **OpenClaw** | 18789 | Personal AI assistant — [openclaw/](openclaw/) |
| **MCP Gateway** | 8811 | Shared MCP tools — [mcp/](mcp/) |
| model-puller | — | Ready to pull Ollama models on demand |
| comfyui-model-puller | — | Ready to download LTX-2 models (~60 GB) on demand |

## First-time setup

1. **Clone** the repo to your target drive (e.g. `F:\AI-toolkit` or `~/AI-toolkit`).
2. **Create `.env`** — copy `.env.example` to `.env` and set `BASE_PATH` to your install path.
3. **Create directories** — run the setup script (also auto-detects GPU and configures compute):
   - **Windows (PowerShell):** `.\scripts\ensure_dirs.ps1`
   - **Linux/Mac:** `./scripts/ensure_dirs.sh`
4. **Start services:** `.\compose.ps1 up -d` (Windows) or `./compose up -d` (Linux/Mac)
5. **Open the dashboard** at [localhost:8080](http://localhost:8080).
6. **Pull models** — use the "Starter pack" button or select models from the dropdown.
7. **Chat** — open [localhost:3000](http://localhost:3000) (Open WebUI).

**No GPU?** Start only the core stack: `.\compose.ps1 up -d ollama dashboard open-webui` — see [Troubleshooting](docs/TROUBLESHOOTING.md).

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

**If ComfyUI or OpenClaw fail:** The dashboard shows troubleshooting hints. ComfyUI uses auto-detected compute (NVIDIA/AMD/Intel/CPU); OpenClaw needs `openclaw/.env` (created by step 3). See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

**On-demand commands** (run when you want to pull models):
- `.\compose.ps1 run --rm model-puller` / `./compose run --rm model-puller` — pull Ollama models from `.env`
- `.\compose.ps1 run --rm comfyui-model-puller` — download LTX-2 models (~60 GB)
- `.\compose.ps1 run --rm openclaw-cli onboard` — OpenClaw setup

## Dashboard

The **dashboard** at [localhost:8080](http://localhost:8080) gives you a single web UI to:

- **View all models** — Ollama (LLM) and ComfyUI (LTX-2) in one place
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

- **Open WebUI** runs with `WEBUI_AUTH=False` by default (no login). Suitable for local/single-user use. If exposing to a network, set `WEBUI_AUTH=True` in the environment.
- **OpenClaw** requires a gateway token — generate with `openssl rand -hex 32`.
- Never commit `.env` or `openclaw/.env`. See [SECURITY.md](SECURITY.md) for details.

## GPU / compute

**Auto-detection:** The setup script (`ensure_dirs`) runs `scripts/detect_hardware.py`, which detects your GPU and generates `docker-compose.compute.yml`:

| Detected | Ollama | ComfyUI |
|----------|--------|---------|
| **NVIDIA** | GPU (NVIDIA Container Toolkit) | CUDA 12.8 |
| **AMD** | ROCm | ROCm |
| **Intel** | CPU | XPU |
| **CPU** | CPU | CPU (slower) |

The `compose` wrapper runs detection before every command, so `.\compose.ps1 up -d` or `./compose up -d` always uses the best settings.

**No GPU?** Run the minimal stack: `.\compose.ps1 up -d ollama dashboard open-webui`. ComfyUI will use CPU by default (slower). See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Data

All data is stored under `BASE_PATH` via bind mounts — no Docker named volumes.

| Path | Contents |
|------|----------|
| `data/ollama` | Ollama models |
| `data/open-webui` | Users, chats, settings |
| `data/comfyui-output` | Generated images/video |
| `data/n8n-data` | Workflows |
| `data/n8n-files` | Shared files |
| `data/openclaw` | OpenClaw config + workspace (SOUL.md, AGENTS.md, TOOLS.md) |
| `models/comfyui/` | LTX-2 models (downloaded on demand) |

## MCP (Model Context Protocol)

The [MCP Gateway](mcp/) exposes shared MCP tools (web search, GitHub, etc.) to all services. Add servers via `MCP_GATEWAY_SERVERS` in `.env`. Connect Open WebUI, Cursor, and OpenClaw to `http://localhost:8811/mcp`. See [mcp/README.md](mcp/README.md).

## OpenClaw

[OpenClaw](openclaw/) is a personal AI assistant, integrated in the main compose. See [openclaw/README.md](openclaw/README.md) for token setup.

## Commands

```powershell
.\compose.ps1 up -d       # Start all services (auto-detects hardware)
.\compose.ps1 logs -f ollama
.\compose.ps1 down        # Stop
```
