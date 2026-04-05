```
  ___          _       
 / _ \ _ __ __| | ___  
| | | | '__/ _` |/ _ \ 
| |_| | | | (_| | (_) |
 \___/|_|  \__,_|\___/

──────────────────────────────────────────────────
Docker Compose stack for local LLMs, chat UI, image/video (ComfyUI), automation (n8n), and OpenClaw — with a unified dashboard.
```

<!--
  Badges (optional): add when repo URL and CI are stable, e.g.:
  [![CI](...)](...)  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
-->

## Overview

**Ordo AI Stack** packages a **local-first** stack: Ollama-backed models behind an **OpenAI-compatible** model-gateway, **Open WebUI** for chat, **ComfyUI** for diffusion workflows, **n8n** for workflows, **OpenClaw** as an optional assistant layer, and an **MCP gateway** for shared tools. A **dashboard** provides a single place to inspect dependencies, pull models, and (with tokens set) control parts of the stack.

**Who it is for:** Operators running the stack on their own machine or LAN; contributors changing Python services, tests, and Compose definitions.

**Docs:** [Getting started](docs/GETTING_STARTED.md) · [Configuration](docs/configuration.md) · [Docker runtime](docs/docker-runtime.md) · [Data](docs/data.md) · [Troubleshooting](docs/runbooks/TROUBLESHOOTING.md) · [Architecture index](docs/architecture/README.md) · [PRD](docs/Product%20Requirements%20Document.md)

## Features

- **Unified dashboard** (port **8080**) — model lists, service links, dependency health, model pulls (when configured).
- **Model gateway** (**11435**) — OpenAI-compatible API in front of Ollama / vLLM backends.
- **Open WebUI** (**3000**) — chat UI.
- **ComfyUI** (**8188**) — workflows; large optional model downloads on demand.
- **n8n** (**5678**) — automation.
- **OpenClaw** (**6680** control UI; **6682** browser bridge) — optional; requires `OPENCLAW_GATEWAY_TOKEN`.
- **MCP gateway** (**8811**) — shared MCP tools for connected clients.
- **Ops controller** (internal **9000**; no host port by default) — compose lifecycle from the dashboard when `OPS_CONTROLLER_TOKEN` is set.
- **GPU profiles** — `scripts/detect_hardware.py` generates `overrides/compute.yml` (gitignored) for NVIDIA / AMD / Intel / CPU paths.

## Quickstart

**Prerequisites:** [Docker](https://docs.docker.com/get-docker/) with Compose, and enough disk for models. For **tests/lint**, Python **3.12+** (see `pyproject.toml`).

1. Clone this repository and open a shell at the repo root.

2. **Environment:** If `.env` is missing, init scripts can create it from `.env.example`. Otherwise copy manually:

   ```bash
   cp .env.example .env
   ```

   Set at least **`BASE_PATH`** to the repo root (see comments in [`.env.example`](.env.example)). Optional: **`DATA_PATH`**, tokens, and model lists.

3. **Full bootstrap** (directories, hardware detection, workspace seeds, then `docker compose up -d --build --force-recreate`):

   **Windows (PowerShell):**

   ```powershell
   cd C:\path\to\ordo-ai-stack
   .\ordo-ai-stack.ps1 initialize
   ```

   **Linux / macOS:**

   ```bash
   cd ~/path/to/ordo-ai-stack
   ./ordo-ai-stack initialize
   ```

4. Open the **dashboard** at [http://localhost:8080](http://localhost:8080) and **Open WebUI** at [http://localhost:3000](http://localhost:3000).

**Lighter bring-up** (no forced rebuild/recreate; runs hardware detection via the `compose` wrapper):

```powershell
.\compose.ps1 up -d
```

```bash
./compose up -d
```

**CPU-only / minimal services:** after init, bring up a subset, e.g. `ollama`, `dashboard`, `open-webui` — see [Troubleshooting](docs/runbooks/TROUBLESHOOTING.md).

## Installation

- **Runtime:** Everything runs in containers; install **Docker** and use the repo from a fixed path (set `BASE_PATH` accordingly).
- **Development:** Python **3.12+**. Install test dependencies:

  ```bash
  pip install -r tests/requirements.txt
  ```

  On Linux/macOS you can use **`make test`**, **`make lint`**, and **`make smoke-test`** (see [Makefile](Makefile)).

## Configuration

Primary reference: **[`.env.example`](.env.example)** (copy to `.env`).

| Area | Variables (examples) |
|------|----------------------|
| Paths | `BASE_PATH`, `DATA_PATH` |
| Models | `MODELS`, `DEFAULT_MODEL` |
| OpenClaw | `OPENCLAW_GATEWAY_TOKEN`, optional Discord/Telegram tokens |
| Security / APIs | `DASHBOARD_AUTH_TOKEN`, `OPS_CONTROLLER_TOKEN`, `WEBUI_AUTH`, `HF_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN` |
| MCP | `MCP_GATEWAY_SERVERS` |
| Compute | `COMPUTE_MODE`, `COMPOSE_FILE` (see comments for `overrides/*.yml`) |
| RAG profile | `EMBED_MODEL`, `QDRANT_PORT`, `RAG_COLLECTION`, … |

Auto-generated: **`overrides/compute.yml`** (from hardware detection). Do not commit secrets; `.env` is gitignored.

## Usage

- **Daily restart / full rebuild:** same as Quickstart step 3 (`ordo-ai-stack initialize`).
- **On-demand one-off containers:**

  ```bash
  ./compose run --rm model-puller
  ./compose run --rm comfyui-model-puller
  ./compose run --rm openclaw-cli onboard
  ./compose --profile openclaude-cli run --rm openclaude-cli
  ```

- **RAG:** `docker compose --profile rag up -d` and ingest paths per [Getting started — RAG](docs/GETTING_STARTED.md#rag-documents-in-chat).
- **MCP clients:** connect to `http://localhost:8811/mcp` (see [mcp/README.md](mcp/README.md)).
- **OpenClaw control UI:** `http://localhost:6680/?token=<OPENCLAW_GATEWAY_TOKEN>` — see [openclaw/README.md](openclaw/README.md) and [openclaw/OPENCLAW_SECURE.md](openclaw/OPENCLAW_SECURE.md).

### Dashboard

The dashboard at [http://localhost:8080](http://localhost:8080) lists models (Ollama and ComfyUI), links to other services, dependency health, and searchable model pulls. With **`OPS_CONTROLLER_TOKEN`** set, it can restart services and run **`POST /api/comfyui/install-node-requirements`** (proxied to ops-controller; use **`DASHBOARD_AUTH_TOKEN`** as in **TROUBLESHOOTING**).

After code changes affecting the dashboard image: `.\compose.ps1 build dashboard` then `.\compose.ps1 up -d` (or `./compose` equivalents).

### Ollama models

Pull lists and defaults come from **`.env`** (`MODELS`, `DEFAULT_MODEL`). Pull via the dashboard or:

```bash
./compose run --rm model-puller
```

### ComfyUI (LTX-2)

Large optional downloads on demand; first run can take a long time. Pull via the dashboard or `./compose run --rm comfyui-model-puller`.

### Security

- **Open WebUI:** set `WEBUI_AUTH=True` when exposing the stack beyond localhost.
- **OpenClaw:** requires `OPENCLAW_GATEWAY_TOKEN`; for restricted access see [openclaw/OPENCLAW_SECURE.md](openclaw/OPENCLAW_SECURE.md) and `overrides/openclaw-secure.yml`.
- **Ops controller:** requires `OPS_CONTROLLER_TOKEN` for dashboard-driven lifecycle and installs.
- Never commit `.env`. Full notes: [SECURITY.md](SECURITY.md).

### GPU / compute

Hardware detection writes **`overrides/compute.yml`**. The `compose` wrapper runs detection before commands. **No GPU:** minimal stack and CPU paths — [Troubleshooting](docs/runbooks/TROUBLESHOOTING.md).

### Architecture

```
User → Dashboard / Open WebUI / N8N / OpenClaw
         │
         ├── Model Gateway (:11435) → Ollama / vLLM
         ├── MCP Gateway (:8811) → shared tools
         └── Ops Controller (:9000) → Docker Compose lifecycle
```

Local-first, OpenAI-compatible endpoint; dashboard does not mount `docker.sock`. Details: [Product Requirements Document](docs/Product%20Requirements%20Document.md).

### Data

Bind mounts only. Set **`BASE_PATH`** (and optionally **`DATA_PATH`**). Ollama blobs under **`models/ollama`**. See [docs/data.md](docs/data.md) and [docs/docker-runtime.md](docs/docker-runtime.md).

### MCP (Model Context Protocol)

[MCP Gateway](mcp/) — configure servers with `MCP_GATEWAY_SERVERS` in `.env`. Endpoint: `http://localhost:8811/mcp`. See [mcp/README.md](mcp/README.md).

### OpenClaw

[openclaw/](openclaw/) is integrated in the main compose. Workspace: **`data/openclaw/workspace/`** (`MEMORY.md`, `TOOLS.md`, `SOUL.md`, `AGENTS.md`, `USER.md`). If **`MEMORY.md`** is not writable or **`TOOLS.md`** is stale, run **`scripts/fix_openclaw_workspace_permissions.ps1`** or **`./scripts/fix_openclaw_workspace_permissions.sh`**, then restart **`openclaw-gateway`**. See [docs/configuration.md](docs/configuration.md) and [Troubleshooting — OpenClaw workspace](docs/runbooks/TROUBLESHOOTING.md#openclaw-workspace--eacces--permission-denied-on-memorymd-or-other-md).

## Development

- Python layout: `dashboard/`, `model-gateway/`, `ops-controller/`, `rag-ingestion/`, `scripts/`; Ruff config in [`pyproject.toml`](pyproject.toml).
- **Do not commit:** `.env`, `data/`, `models/`, `overrides/compute.yml`, `mcp/.env` — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Testing

```bash
pip install -r tests/requirements.txt
python -m pytest tests/ -v
python -m ruff check dashboard tests model-gateway ops-controller rag-ingestion scripts
```

**OpenClaw config check** (example fixture path used in CI):

```bash
python scripts/validate_openclaw_config.py tests/fixtures/openclaw_valid.json
```

**Health / diagnostics:**

```powershell
.\scripts\doctor.ps1
```

```bash
./scripts/doctor.sh
```

Optional: `DOCTOR_DEPS_TIMEOUT_SEC`; `DASHBOARD_AUTH_TOKEN` from `.env` when probing the dashboard. See [Troubleshooting — Quick Diagnostics](docs/runbooks/TROUBLESHOOTING.md).

**Smoke (Docker required):**

```powershell
.\scripts\smoke_test.ps1
```

```bash
./scripts/smoke_test.sh
# or: make smoke-test
```

**CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): TruffleHog secret scan; **pytest** + **ruff**; OpenClaw fixture validation; **`docker compose config`**; optional **compose smoke** via workflow dispatch.

## Troubleshooting

1. **Services won’t start or images are stale** — Rebuild affected images and recreate, e.g. `docker compose build dashboard model-gateway` (or the `compose` wrapper), then `up -d`. Doctor **WARN** on missing `/api/dependencies` or `/ready` often indicates an old image.
2. **Doctor warns on Ollama (11434) or MCP (8811)** — Expected if those ports are not published; use `overrides/ollama-expose.yml` / `overrides/mcp-expose.yml` or set `DOCTOR_STRICT=1` only when you intend strict probes (see doctor script comments in repo).
3. **No GPU** — Use a minimal service set or CPU-oriented overrides; ComfyUI will be slower.
4. **OpenClaw workspace `EACCES` or stale `TOOLS.md`** — Run `scripts/fix_openclaw_workspace_permissions.ps1` or `./scripts/fix_openclaw_workspace_permissions.sh`, then restart `openclaw-gateway` — [Troubleshooting — OpenClaw](docs/runbooks/TROUBLESHOOTING.md#openclaw-workspace--eacces--permission-denied-on-memorymd-or-other-md).
5. **Exposing to a network** — Enable **Open WebUI** auth (`WEBUI_AUTH=True`), protect **OpenClaw** with the gateway token, and harden **n8n** — see [SECURITY.md](SECURITY.md).

More: [docs/runbooks/TROUBLESHOOTING.md](docs/runbooks/TROUBLESHOOTING.md) · [BACKUP_RESTORE.md](docs/runbooks/BACKUP_RESTORE.md) · [UPGRADE.md](docs/runbooks/UPGRADE.md)

## Roadmap

Rolling changes: [CHANGELOG.md](CHANGELOG.md).

## Contributing

See **[CONTRIBUTING.md](CONTRIBUTING.md)**. Report security issues per **[SECURITY.md](SECURITY.md)** (do not use public issues for vulnerabilities).

## License

[MIT License](LICENSE) — Copyright (c) 2026 Ordo AI Stack contributors.
