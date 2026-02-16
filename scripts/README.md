# Scripts

Setup and maintenance scripts for the LLM-toolkit stack.

## Setup

| Script | Purpose |
|--------|---------|
| `ensure_dirs.ps1` | Creates all data directories (`data/`, `models/`) for bind mounts. Run before first `docker compose up`. |

## ComfyUI

| Script | Purpose |
|--------|---------|
| `comfyui/pull_comfyui_models.py` | Downloads LTX-2 models from Hugging Face. Run by `comfyui-model-puller` on first start, or manually: `docker compose run --rm comfyui-model-puller`. |

## Usage

From the repo root:

```powershell
# Set project path (Windows)
$env:BASE_PATH = "F:/LLM-toolkit"
$env:DATA_PATH = "F:/LLM-toolkit/data"   # optional override

# Create directories
.\scripts\ensure_dirs.ps1

# OpenClaw workspace (if using openclaw/)
.\openclaw\scripts\ensure_openclaw_workspace.ps1
```
