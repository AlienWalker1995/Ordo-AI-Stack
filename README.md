```
  ___          _       
 / _ \ _ __ __| | ___  
| | | | '__/ _` |/ _ \ 
| |_| | | | (_| | (_) |
 \___/|_|  \__,_|\___/

──────────────────────────────────────────────────
Docker Compose stack for local LLMs, chat UI, image/video (ComfyUI), and automation (n8n) — with a unified dashboard.
```

<!--
  Badges (optional): add when repo URL and CI are stable, e.g.:
  [![CI](...)](...)  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
-->

## Overview

**Ordo AI Stack** packages a **local-first, operator-deployed** stack: llama.cpp-backed models behind an **OpenAI-compatible** LiteLLM model gateway, **Open WebUI** for chat, **ComfyUI** for diffusion workflows, **n8n** for workflows, and an **MCP gateway** for shared tools. A **dashboard** provides a single place to inspect dependencies, pull models, and control the stack.

**Deployment model:** Single homelab operator. All user-facing UIs sit behind a **Caddy + oauth2-proxy + Tailscale + Google SSO** front door — no UI service publishes a host port directly. The operator brings their own Tailscale tailnet and Google OAuth client; the stack stitches them together so every UI is reachable at `https://${CADDY_TAILNET_HOSTNAME}/<service>/` after a single Google sign-in, with an email allowlist gating access. See [docs/runbooks/auth.md](docs/runbooks/auth.md) for the one-time setup.

**Who it is for:** A homelab operator running the stack on their own hardware, exposed over their tailnet to a small allowlist of personal Google accounts. Local AI models, strong operator-deployment principles.

**Docs:** [Getting started](docs/GETTING_STARTED.md) · [Auth front door](docs/runbooks/auth.md) · [Secrets](docs/runbooks/secrets.md) · [Configuration](docs/configuration.md) · [Data](docs/data.md) · [Hermes Agent](docs/hermes-agent.md) · [PRD index](docs/product%20requirements%20docs/index.md)

## Features

All UI ports below are **internal** (container-network). Operators reach them via the Caddy front door under `https://${CADDY_TAILNET_HOSTNAME}/<path>/`; the only host-published ports are Caddy `:443` (tailnet-bound), and `127.0.0.1`-bound publishes of `model-gateway:11435`, `mcp-gateway:8811`, and `qdrant:6333` for host-side tools (Cursor, Cline, scripts).

- **Unified dashboard** (internal **8080**, front-door `/dash/`) — model lists, service links, dependency health, model pulls.
- **Model gateway** (host `127.0.0.1:11435`, also internal) — LiteLLM OpenAI-compatible API in front of llama.cpp backends.
- **Open WebUI** (internal **8080**, front-door `/`) — chat UI at the root of the tailnet hostname.
- **ComfyUI** (internal **8188**, front-door `/comfy/`) — workflows; large optional model downloads on demand.
- **n8n** (internal **5678**, front-door `/n8n/`) — automation.
- **MCP gateway** (host `127.0.0.1:8811`, also internal) — shared MCP tools for host clients and in-stack services.
- **Ops controller** (internal **9000**; no host port) — compose lifecycle from the dashboard with `OPS_CONTROLLER_TOKEN`.
- **Hermes dashboard** (internal **9119**, front-door `/hermes/`) — assistant-agent UI.
- **GPU profiles** — `scripts/detect_hardware.py` generates `overrides/compute.yml` (gitignored) for NVIDIA / AMD / Intel / CPU paths.

## Quickstart

**Prerequisites:**

- [Docker](https://docs.docker.com/get-docker/) with Compose, and enough disk for models.
- [Tailscale](https://tailscale.com/) installed on the host machine, with a Tailscale-issued TLS cert for the chosen tailnet hostname (`tailscale cert ordo.<tailnet>.ts.net`).
- A Google Cloud OAuth 2.0 Web client for the SSO front door (Client ID + secret).
- [SOPS](https://github.com/getsops/sops) + [age](https://github.com/FiloSottile/age) for secrets at rest.
- For **tests / lint**, Python **3.12+** (see `pyproject.toml`).

1. Clone this repository and open a shell at the repo root.

2. **Environment:** If `.env` is missing, init scripts can create it from `.env.example`. Otherwise copy manually:

   ```bash
   cp .env.example .env
   ```

   Set at least **`BASE_PATH`**, **`CADDY_BIND`** (your tailnet IPv4 from `tailscale ip -4`), and **`CADDY_TAILNET_HOSTNAME`** (e.g. `ordo.<tailnet>.ts.net`). See comments in [`.env.example`](.env.example).

3. **Auth front door (one-time):** Follow [docs/runbooks/auth.md](docs/runbooks/auth.md) to configure the Tailscale cert, Google OAuth client, cookie secret, and email allowlist.

4. **Secrets (one-time):** Follow [docs/runbooks/secrets.md](docs/runbooks/secrets.md) — generate an age keypair, register your public key in `secrets/.sops.yaml`, and run `make decrypt-secrets` to materialize runtime tokens at `~/.ai-toolkit/runtime/secrets/`.

5. **Full bring-up** — the `compose` wrapper runs hardware detection, then builds and starts the stack:

   **Windows (PowerShell):**

   ```powershell
   .\compose.ps1 up -d --build --force-recreate
   ```

   **Linux / macOS:**

   ```bash
   ./compose up -d --build --force-recreate
   ```

6. From any device on your tailnet, browse to `https://${CADDY_TAILNET_HOSTNAME}/` — Google sign-in gates the front door, then Open WebUI loads at `/`, the dashboard at `/dash/`, n8n at `/n8n/`, ComfyUI at `/comfy/`, and the Hermes UI at `/hermes/`.

**Lighter bring-up** (no forced rebuild/recreate; still runs hardware detection):

```powershell
.\compose.ps1 up -d
```

```bash
./compose up -d
```

**CPU-only / minimal services:** bring up a subset after init, e.g. `./compose up -d llamacpp dashboard open-webui`.

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
| Security / APIs | `DASHBOARD_AUTH_TOKEN`, `OPS_CONTROLLER_TOKEN`, `WEBUI_AUTH`, `HF_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN` |
| MCP | `MCP_GATEWAY_SERVERS` |
| Compute | `COMPUTE_MODE`, `COMPOSE_FILE` (see comments for `overrides/*.yml`) |
| RAG profile | `EMBED_MODEL`, `QDRANT_PORT`, `RAG_COLLECTION`, … |

Auto-generated: **`overrides/compute.yml`** (from hardware detection). Do not commit secrets; `.env` is gitignored.

## Usage

- **Daily restart / full rebuild:** same as Quickstart step 3.
- **On-demand one-off containers:**

  ```bash
  ./compose run --rm model-puller
  ./compose run --rm comfyui-model-puller
  ```

- **RAG:** `docker compose --profile rag up -d` and ingest paths per [Getting started — RAG](docs/GETTING_STARTED.md#rag-documents-in-chat).
- **MCP clients:** connect to `http://localhost:8811/mcp` (see [mcp/README.md](mcp/README.md)).

### Dashboard

Reach the dashboard at `https://${CADDY_TAILNET_HOSTNAME}/dash/` (Google SSO front door; allowlist via `auth/oauth2-proxy/emails.txt`). It lists models (GGUF/llama.cpp and ComfyUI), links to other services, dependency health, and Hugging Face model pulls. **`OPS_CONTROLLER_TOKEN`** lets it restart services and run **`POST /api/comfyui/install-node-requirements`**. **`DASHBOARD_AUTH_TOKEN`** is an optional bearer layer for non-browser API access; the browser path is gated by SSO at the proxy level.

After code changes affecting the dashboard image: `.\compose.ps1 build dashboard` then `.\compose.ps1 up -d` (or `./compose` equivalents).

### LLM models (GGUF / llama.cpp)

The stack pulls GGUF files (served by llama.cpp) directly from Hugging Face. Repo lists and defaults come from **`.env`** (`GGUF_MODELS`, `DEFAULT_MODEL`). Pull via the dashboard's **Models** panel (enter a Hugging Face repo id, a `huggingface.co/…`/`.gguf` URL, or `.env` to pull all `GGUF_MODELS`), or from the CLI:

```bash
./compose run --rm gguf-puller
```

### ComfyUI (LTX-2)

Large optional downloads on demand; first run can take a long time. Pull via the dashboard or `./compose run --rm comfyui-model-puller`.

### Security

- **Front door:** Caddy + oauth2-proxy + Google SSO gates all browser-reachable UIs at the network edge. Email allowlist in `auth/oauth2-proxy/emails.txt` (replace `YOUR_ALLOWLIST_EMAIL` locally — never commit your real email). See [docs/runbooks/auth.md](docs/runbooks/auth.md).
- **Open WebUI:** runs with native auth disabled by default because Google SSO already gates it at the proxy; flip `WEBUI_AUTH=True` if you want a second auth layer for multi-user workspaces.
- **Dashboard:** `DASHBOARD_AUTH_TOKEN` provides a bearer-token fallback for non-browser API access (e.g. host scripts). Browser traffic is SSO-gated.
- **Ops controller:** requires `OPS_CONTROLLER_TOKEN` for dashboard-driven lifecycle and installs; no host port at all.
- **Secret management:** SOPS + age. Only encrypted `secrets/*.sops` blobs and architecture/config are committed; **plaintext is decrypted on the host only**, into `~/.ai-toolkit/runtime/` (outside every container's reach), and never enters the repo or a chat/log. Env-form secrets load via two `--env-file`s (`.env` defaults + `runtime/.env`, last-wins); high-value tokens mount as Docker secrets at `/run/secrets/<name>`. The ops-controller mounts `runtime/.env` read-only so it can recreate secret-dependent services with real values. See [docs/runbooks/secrets.md](docs/runbooks/secrets.md).
- Never commit `.env` or any plaintext secret, and never synthesize placeholder secret values to clear an error — decrypt on the host instead. Full notes: [SECURITY.md](SECURITY.md).

### GPU / compute

Hardware detection writes **`overrides/compute.yml`**. The `compose` wrapper runs detection before commands. **No GPU:** use a minimal service set (`./compose up -d llamacpp dashboard open-webui`); ComfyUI will be slower.

### Architecture

```
Tailnet device → Caddy :443 (TLS) → oauth2-proxy (Google SSO + email allowlist)
                                          │
                                          ├── /          → Open WebUI
                                          ├── /dash/     → Dashboard
                                          ├── /n8n/      → n8n
                                          ├── /comfy/    → ComfyUI
                                          └── /hermes/   → Hermes dashboard
                                                  │
                                                  ├── Model Gateway → LiteLLM → llama.cpp
                                                  ├── MCP Gateway → shared tools (SearXNG, n8n, ComfyUI, …)
                                                  └── Ops Controller → Docker Compose lifecycle (token-auth, no host port)
```

Local-first AI; operator-deployed front door. Dashboard does not mount `docker.sock`. Details: [PRD index](docs/product%20requirements%20docs/index.md).

### Data

Bind mounts only. Set **`BASE_PATH`** (and optionally **`DATA_PATH`**). See [docs/data.md](docs/data.md).

### MCP (Model Context Protocol)

[MCP Gateway](mcp/) — configure servers with `MCP_GATEWAY_SERVERS` in `.env`. Endpoint: `http://localhost:8811/mcp`. See [mcp/README.md](mcp/README.md).

### Hermes Agent

Hermes Agent runs as two compose services (`hermes-gateway` + `hermes-dashboard`) with persistent state under `data/hermes/`. Setup and upgrade notes: [docs/hermes-agent.md](docs/hermes-agent.md).

## Development

- Python layout: `dashboard/`, `model-gateway/`, `ops-controller/`, `rag-ingestion/`, `scripts/`; Ruff config in [`pyproject.toml`](pyproject.toml).
- **Do not commit:** `.env`, `data/`, `models/`, `overrides/compute.yml`, `mcp/.env` — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Testing

```bash
pip install -r tests/requirements.txt
python -m pytest tests/ -v
python -m ruff check dashboard tests model-gateway ops-controller rag-ingestion scripts comfyui-mcp orchestration-mcp worker
```

**Health / diagnostics:**

```powershell
.\scripts\doctor.ps1
```

```bash
./scripts/doctor.sh
```

Optional: `DOCTOR_DEPS_TIMEOUT_SEC`; `DASHBOARD_AUTH_TOKEN` from `.env` when probing the dashboard.

**Smoke (Docker required):**

```powershell
.\scripts\smoke_test.ps1
```

```bash
./scripts/smoke_test.sh
# or: make smoke-test
```

**CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): TruffleHog secret scan; **pytest** + **ruff**; **`docker compose config`**; optional **compose smoke** via workflow dispatch.

## Troubleshooting

1. **Services won’t start or images are stale** — Rebuild affected images and recreate, e.g. `docker compose build dashboard model-gateway` (or the `compose` wrapper), then `up -d`. Doctor **WARN** on missing `/api/dependencies` or `/ready` often indicates an old image.
2. **Doctor warns on MCP (8811)** — Expected if that port is not published; use `overrides/mcp-expose.yml` or set `DOCTOR_STRICT=1` only when you intend strict probes (see doctor script comments in repo).
3. **No GPU** — Use a minimal service set or CPU-oriented overrides; ComfyUI will be slower.
4. **Exposing to a network** — Enable **Open WebUI** auth (`WEBUI_AUTH=True`), set `DASHBOARD_AUTH_TOKEN`, and harden **n8n** — see [SECURITY.md](SECURITY.md).

## Roadmap

Rolling changes: [CHANGELOG.md](CHANGELOG.md).

## Contributing

See **[CONTRIBUTING.md](CONTRIBUTING.md)**. Report security issues per **[SECURITY.md](SECURITY.md)** (do not use public issues for vulnerabilities).

## License

[MIT License](LICENSE) — Copyright (c) 2026 Ordo AI Stack contributors.
