# Scripts

Setup, operations, and maintenance scripts for the Ordo AI Stack.

## Setup

| Script | Purpose |
|--------|---------|
| `ensure_dirs.sh` / `.ps1` | Creates all data directories (`data/`, `models/`) for bind mounts, bootstraps configs, detects hardware. **Run before first `docker compose up`.** |
| `detect_hardware.py` | Detects GPU (NVIDIA/AMD/Intel/Apple Silicon/CPU), generates `overrides/compute.yml`, writes `.wslconfig` (Windows/WSL). Called automatically by `ensure_dirs` and `compose`. |

## Health and Diagnostics

| Script | Purpose |
|--------|---------|
| `doctor.sh` / `.ps1` | Deep health probes (dashboard, model-gateway, MCP gateway). |
| `smoke_test.sh` / `.ps1` | Quick smoke test: optionally starts services, then checks health endpoints. Also in `Makefile`. |

## MCP Gateway

| Script | Purpose |
|--------|---------|
| `mcp_add.sh` / `.ps1` | Add an MCP server (e.g. `./scripts/mcp_add.sh fetch`). Gateway reloads in ~10s without container restart. |
| `mcp_remove.sh` / `.ps1` | Remove an MCP server. Gateway reloads in ~10s. |

## Security

| Script | Purpose |
|--------|---------|
| `ssrf-egress-block.sh` | iptables rules blocking SSRF from MCP / agent containers to private ranges and cloud metadata. Linux only. |
| `ssrf-egress-block.ps1` | Windows guidance (prints options; actual blocking requires WSL iptables). |

## ComfyUI

| Script | Purpose |
|--------|---------|
| `comfyui/pull_comfyui_models.py` | Config-driven model downloader. Run by `comfyui-model-puller` service, or manually: `docker compose --profile comfyui-models run --rm comfyui-model-puller`. |
| `comfyui/models.json` | Model pack definitions for the downloader. |
| `comfyui/install_node_requirements.sh` / `.ps1` | Install pip requirements for a ComfyUI custom node into the running container. |
| `comfyui/validate_comfyui_pipeline.py` | Diagnostic: validates ComfyUI host paths, checkpoints, workflow refs, and HTTP connectivity. |

## Model Downloads

| Script | Purpose |
|--------|---------|
| `pull_gguf_models.py` | Downloads GGUF files from HuggingFace. Used by the `model-puller` Docker service. |

## Usage

From the repo root:

**Windows (PowerShell):**
```powershell
$env:BASE_PATH = "F:/ordo-ai-stack"
.\scripts\ensure_dirs.ps1
```

**Linux/Mac:**
```bash
export BASE_PATH="$HOME/ordo-ai-stack"
./scripts/ensure_dirs.sh
```
