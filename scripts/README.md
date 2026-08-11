# Scripts

> ⚠️ **Mixed lifecycle — read per-row.** Some of these scripts are LIVE (bind-mounted into containers, invoked by Hermes cron, or the documented secrets flow); only the V1 *bring-up* model is retired:  `ensure_dirs` + `detect_hardware.py` (→ `overrides/compute.yml`), the `./compose` / `.\compose.ps1` wrapper, and the model pullers. That V1 top-level tree (root `docker-compose.yml`, `compose`/`compose.ps1`, the root `Makefile`, `overrides/`, `scripts/detect_hardware.py`, the root `.env.example`) was **removed 2026-07-24** after the 2026-07-09 v2 cutover soak — it is no longer present in this repo — and its last remnant, the `ensure_dirs` scripts + the ComfyUI-Manager seed they bootstrapped, have now been removed too, so every script listed below is live. In production, the stack is defined and operated entirely from the repo root (edit `ordo.yaml`, run `ordo render`, bring up the rendered compose from `out/`); directory/config generation is the render engine, hardware detection is `ordo detect` (`hardware: auto`), model provisioning is **`ordo fetch`** (checksum-mandatory, offline-capable — replaces `pull_gguf_models.py` / the pullers), and GPU scheduling is `ordo serve` (not a reactive guardian). See [`../docs/operator-guide.md`](../docs/operator-guide.md).

Setup, operations, and maintenance scripts for the Ordo AI Stack.

## Live at runtime (do NOT treat as legacy)

| Script | Role |
| --- | --- |
| `llamacpp/run-llama-server.sh` | **Bind-mounted entrypoint** of the llamacpp container (`${BASE_PATH}/scripts/llamacpp:/llamacpp-scripts:ro` in the rendered compose). Deleting it takes the LLM down on next restart. |
| `stack_monitor.py` | Daily image/pin audit — invoked by Hermes cron via the `/c/dev` mirror mount; tested by `tests/test_stack_monitor_*.py`. |
| `storage_purge.py` | Weekly storage purge — invoked by Hermes cron (`cd /c/dev/ordo-ai-stack && python3 scripts/storage_purge.py`); tested by `tests/test_storage_purge.py`. |
| `secrets/decrypt.sh`, `secrets/rotate-internal.sh`, `secrets/audit-git-history.sh` | The documented secrets flow (`docs/runbooks/secrets.md`); decrypt materializes the host files Docker secrets mount from. |
| `comfyui/pull_comfyui_models.py` + `comfyui/models.json` | Spawned by the live dashboard (`app.py`) for ComfyUI model-pack downloads. |

## Health and Diagnostics

| Script | Purpose |
|--------|---------|
| `smoke_test.sh` / `.ps1` | Quick smoke test against the rendered `out/docker-compose.yml` (project `ordo`): optionally starts services, then checks health in-network via `docker compose exec` (only Caddy publishes a host port). |

## MCP Gateway

| Script | Purpose |
|--------|---------|
| `mcp_add.sh` / `.ps1` | Add an MCP server (e.g. `./scripts/mcp_add.sh fetch`). Edits `out/mcp/servers.txt` (mounted into `ordo-mcp-gateway-1`); gateway reloads in ~10s without container restart. |
| `mcp_remove.sh` / `.ps1` | Remove an MCP server. Edits `out/mcp/servers.txt`; gateway reloads in ~10s. |

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

## Model Downloads

GGUF model provisioning is **`ordo fetch`** (checksum-mandatory, offline-capable). The V1
`pull_gguf_models.py` script and its `model-puller` one-shot Docker service are gone;
`ordo/ops-api` no longer shells out to either (`/models/gguf-pull` now returns 501).

## n8n

| Script | Purpose |
|--------|---------|
| `n8n/bootstrap_owner.py` | Applies `N8N_OWNER_EMAIL` / `N8N_OWNER_PASSWORD` (env-form secrets in `out/secrets.env`, see `docs/runbooks/secrets.md`) to the running n8n instance — first-run owner bootstrap, or updates the existing owner's email/password on re-run. Idempotent; run from inside the docker network so `n8n:5678` is reachable. |

## Usage

From the repo root:

**Windows (PowerShell):**
```powershell
$env:BASE_PATH = "F:/ordo-ai-stack"
.\scripts\smoke_test.ps1
```

**Linux/Mac:**
```bash
export BASE_PATH="$HOME/ordo-ai-stack"
./scripts/smoke_test.sh
```
