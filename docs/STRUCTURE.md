# Repository Structure

```
LLM-toolkit/
├── docker-compose.yml      # Main stack: Ollama, Open WebUI, ComfyUI, n8n
├── .env.example            # Copy to .env — BASE_PATH, MODELS
├── README.md               # Quick start, services, commands
│
├── scripts/                # Setup and model pullers
│   ├── ensure_dirs.ps1     # Creates data/ and models/ directories
│   └── comfyui/
│       └── pull_comfyui_models.py  # LTX-2 model downloader
│
├── openclaw/               # Optional: Personal AI assistant
│   ├── docker-compose.yml  # OpenClaw gateway + CLI
│   ├── .env.example        # OpenClaw-specific env
│   ├── README.md           # OpenClaw setup guide
│   ├── workspace/          # SOUL.md, AGENTS.md, TOOLS.md templates
│   └── scripts/
│       └── ensure_openclaw_workspace.ps1
│
├── data/                   # Runtime data (gitignored)
│   ├── ollama/
│   ├── open-webui/
│   ├── comfyui-storage/
│   ├── comfyui-output/
│   ├── n8n-data/
│   ├── n8n-files/
│   └── openclaw/
│       └── workspace/
│
├── models/                 # Model files (gitignored)
│   └── comfyui/
│
└── docs/
    └── STRUCTURE.md        # This file
```

## Data paths

All services use bind mounts under `BASE_PATH` (no Docker named volumes):

| Path | Purpose |
|------|---------|
| `data/ollama` | Ollama models |
| `data/open-webui` | Chat UI data |
| `data/comfyui-storage` | ComfyUI install + custom nodes |
| `data/comfyui-output` | Generated images/videos |
| `data/n8n-data` | Workflows |
| `data/n8n-files` | Shared files |
| `data/openclaw` | OpenClaw config + workspace |
| `models/comfyui` | LTX-2 checkpoints, LoRAs, text encoders |
