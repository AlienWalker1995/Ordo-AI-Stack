# LLM-toolkit

Hey, I am Cam, I made this repo to simplify my local-LLM setup.

Ollama + Open WebUI + ComfyUI + N8N in Docker. One command, all on one drive.

→ [Repository structure](docs/STRUCTURE.md)

## Services

| Service | Port | Purpose |
|---------|------|---------|
| **ollama** | 11434 | Local LLM runtime (GPU) |
| **open-webui** | 3000 | Chat UI — [localhost:3000](http://localhost:3000) |
| **comfyui** | 8188 | Stable Diffusion / LTX-2 — [localhost:8188](http://localhost:8188) |
| **n8n** | 5678 | Workflow automation — [localhost:5678](http://localhost:5678) |
| **OpenClaw** | 18789 | Personal AI assistant — [openclaw/](openclaw/) |
| model-puller | — | Pulls Ollama models once on first start |
| comfyui-model-puller | — | Downloads LTX-2 models (~60 GB) once on first start |

## Quick start

```powershell
# 1. Clone / copy to your target drive (repo name: LLM-toolkit)
cd F:\LLM-toolkit

# 2. Create .env (edit BASE_PATH to match your install path)
copy .env.example .env

# 3. Create data directories
.\scripts\ensure_dirs.ps1

# 4. Start — pick what you need via profiles
docker compose --profile ollama --profile openclaw up -d   # OpenClaw + Ollama only
# docker compose --profile ollama --profile openclaw --profile webui up -d   # + chat UI
# docker compose --profile ollama --profile openclaw --profile models up -d  # + pull models
# docker compose --profile ollama --profile openclaw --profile comfyui up -d  # + ComfyUI
# docker compose --profile ollama --profile openclaw --profile n8n up -d     # + n8n
```

| Profiles | Services |
|----------|----------|
| `ollama` | Ollama LLM runtime |
| `openclaw` | OpenClaw gateway — [localhost:18789](http://localhost:18789) |
| `webui` | Open WebUI chat — [localhost:3000](http://localhost:3000) |
| `models` | Pull Ollama models on first start |
| `comfyui` | ComfyUI + LTX-2 models |
| `n8n` | N8N workflow automation |

## Ollama models

Default models (set in `.env`):

- `deepseek-r1:7b` — reasoning
- `deepseek-coder:6.7b` — coding
- `nomic-embed-text` — embeddings / RAG

Change them in `.env` and re-pull:

```bash
docker compose up -d model-puller
```

## ComfyUI (LTX-2)

ComfyUI waits for `comfyui-model-puller` to finish downloading LTX-2 models (~60 GB). First run takes a while; subsequent runs skip existing files.

**Auto-downloaded:** LTX-2 checkpoint (fp8), LoRAs, latent upscaler, Gemma 3 12B text encoder.

Re-pull models:

```bash
docker compose up -d comfyui-model-puller
```

## Security

- **Open WebUI** runs with `WEBUI_AUTH=False` by default (no login). Suitable for local/single-user use. If exposing to a network, set `WEBUI_AUTH=True` in the environment.
- **OpenClaw** requires a gateway token — generate with `openssl rand -hex 32`.
- Never commit `.env` or `openclaw/.env`. See [SECURITY.md](SECURITY.md) for details.

## GPU

The `ollama` and `comfyui` services are configured for NVIDIA GPU via the Container Toolkit. Remove the `deploy` block if you don't have a GPU.

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
| `models/comfyui/` | LTX-2 models (auto-downloaded) |

## OpenClaw

[OpenClaw](openclaw/) is a personal AI assistant, now integrated in the main compose. Use `--profile openclaw` (with `--profile ollama`). See [openclaw/README.md](openclaw/README.md) for token setup.

## Commands

```powershell
docker compose --profile ollama --profile openclaw up -d   # Ollama + OpenClaw
docker compose logs -f ollama                              # View logs
docker exec llm-toolkit-ollama-1 ollama pull <model>   # Pull a model
docker compose down                                        # Stop
```
