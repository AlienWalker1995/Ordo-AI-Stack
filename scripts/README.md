# Scripts

> ⚠️ **LEGACY (V1) — retired.** These scripts drove the V1 bring-up model: `ensure_dirs` + `detect_hardware.py` (→ `overrides/compute.yml`), the `./compose` / `.\compose.ps1` wrapper, and the model pullers. That V1 top-level tree (root `docker-compose.yml`, `compose`/`compose.ps1`, the root `Makefile`, `overrides/`, `scripts/detect_hardware.py`, the root `.env.example`) was **removed 2026-07-24** after the 2026-07-09 v2 cutover soak — it is no longer present in this repo, so several rows below are orphaned. In production **v2**, the stack is defined and operated entirely from `v2/` (edit `v2/ordo.yaml`, run `ordo render`, bring up the rendered compose from `v2/out/`); directory/config generation is the render engine, hardware detection is `ordo detect` (`hardware: auto`), model provisioning is **`ordo fetch`** (checksum-mandatory, offline-capable — replaces `pull_gguf_models.py` / the pullers), and GPU scheduling is `ordo serve` (not a reactive guardian). See [`../v2/README.md`](../v2/README.md) (+ [`../v2/CUTOVER.md`](../v2/CUTOVER.md)) and [`../docs/LEGACY-CLEANUP.md`](../docs/LEGACY-CLEANUP.md).

Setup, operations, and maintenance scripts for the Ordo AI Stack.

## Setup

| Script | Purpose |
|--------|---------|
| `ensure_dirs.sh` / `.ps1` | Creates all data directories (`data/`, `models/`) for bind mounts, bootstraps configs. Written for the V1 bring-up model; its hardware-detection step called `detect_hardware.py`, which was removed along with the rest of the V1 tree. In v2, hardware detection and config generation happen at `ordo render` time (`hardware: auto` / `ordo detect`), rendered into `v2/out/`. |

## Health and Diagnostics

| Script | Purpose |
|--------|---------|
| `doctor.sh` / `.ps1` | Deep health probes (dashboard, model-gateway, MCP gateway). |
| `smoke_test.sh` / `.ps1` | Quick smoke test: optionally starts services, then checks health endpoints. |

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
| `comfyui/pull_comfyui_models.py` | Config-driven model downloader. V1 wired this to a `comfyui-model-puller` one-shot compose service (profile `comfyui-models`); that service was retired as obsolete-by-design in v2 (provisioning now belongs to the operator's ComfyUI image / `ordo fetch`-style flow) — run it standalone instead: `python scripts/comfyui/pull_comfyui_models.py`. |
| `comfyui/models.json` | Model pack definitions for the downloader. |
| `comfyui/install_node_requirements.sh` / `.ps1` | Install pip requirements for a ComfyUI custom node into the running container. |
| `comfyui/validate_comfyui_pipeline.py` | Diagnostic: validates ComfyUI host paths, checkpoints, workflow refs, and HTTP connectivity. |

## Model Downloads

| Script | Purpose |
|--------|---------|
| `pull_gguf_models.py` | Downloads GGUF files from HuggingFace. V1 used this via a `model-puller` one-shot Docker service; that service is gone in v2, which provisions GGUF models via **`ordo fetch`** (checksum-mandatory, offline-capable) instead. |

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
