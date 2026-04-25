```
  ___          _       
 / _ \ _ __ __| | ___  
| | | | '__/ _` |/ _ \ 
| |_| | | | (_| | (_) |
 \___/|_|  \__,_|\___/

──────────────────────────────────────────────────
Docker Compose stack for local LLMs, chat UI, image/video (ComfyUI), and automation (n8n) — with a unified dashboard, single-sign-on front door, and an opinionated Hermes agent.
```

<!--
  Badges (optional): add when repo URL and CI are stable, e.g.:
  [![CI](...)](...)  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
-->

## Overview

**Ordo AI Stack** packages a **local-first** stack: llama.cpp behind an **OpenAI-compatible** LiteLLM model gateway, **Open WebUI** for chat, **ComfyUI** for diffusion workflows, **n8n** for workflows, an **MCP gateway** for shared tools, and the **Hermes agent** for agentic work. A **dashboard** provides a single place to inspect dependencies, pull models, and (with `OPS_CONTROLLER_TOKEN` set) drive compose lifecycle through an audited HTTP API.

The stack is designed to live behind **Tailscale** as the network gate, with a Caddy + oauth2-proxy front door doing **Google SSO** for the dashboard. Secrets at rest are encrypted with **SOPS + age**; high-value tokens are mounted into containers as **Docker secrets** so they never appear in `docker inspect`.

**Who it is for:** Operators running the stack on their own machine or LAN; contributors changing Python services, tests, and Compose definitions.

**Docs:** [Getting started](docs/GETTING_STARTED.md) · [Configuration](docs/configuration.md) · [Data](docs/data.md) · [Hermes Agent](docs/hermes-agent.md) · [Auth runbook](docs/runbooks/auth.md) · [Secrets runbook](docs/runbooks/secrets.md) · [PRD index](docs/product%20requirements%20docs/index.md)

## Features

- **Unified dashboard** (`/dash/` behind SSO; backend port `8080`) — model lists, service links, dependency health, model pulls, throughput panel.
- **SSO front door** — Caddy + oauth2-proxy + Google OIDC, allowlisted by `auth/oauth2-proxy/emails.txt`. Bound to your Tailscale IP via `CADDY_BIND`. See [docs/runbooks/auth.md](docs/runbooks/auth.md).
- **Encrypted secrets at rest** — SOPS + age, public-key recipient in `secrets/.sops.yaml`. High-value tokens (Discord, GitHub PAT, HF, Tavily, Civitai) are Docker secrets, not env vars. See [docs/runbooks/secrets.md](docs/runbooks/secrets.md).
- **Model gateway** (`11435`) — LiteLLM OpenAI-compatible API in front of llama.cpp.
- **llama.cpp** (backend-only) — runs the chat model. Default build is the [TurboQuant](https://github.com/AmesianX/TurboQuant) fork with `tbq*`/`tbqp*` KV-cache types for Blackwell sm_120; falls back cleanly to upstream `llama.cpp:server-cuda` if you set `LLAMACPP_IMAGE`.
- **Open WebUI** (`3000`) — chat UI; reached directly on the tailnet (SPA, can't be sub-path mounted).
- **ComfyUI** (`8188`) — diffusion workflows; large optional model downloads on demand.
- **n8n** (`5678`) — automation.
- **MCP gateway** (`8811`) — shared MCP tools for connected clients (Cursor, Claude Desktop, Hermes).
- **Hermes agent** (`9119`) — assistant agent with FTS-indexed memory, scheduled jobs, Discord bridge.
- **Ops controller** (internal `9000`; no host port) — audited compose lifecycle. Every privileged call appends one JSONL line to `data/ops-controller/audit.jsonl` (rotated at 50 MB).
- **GPU profiles** — `scripts/detect_hardware.py` generates `overrides/compute.yml` (gitignored) for NVIDIA / AMD / Intel / CPU paths.

## Quickstart

**Prerequisites:** [Docker](https://docs.docker.com/get-docker/) with Compose; for tests/lint, Python **3.12+** (see `pyproject.toml`); for SOPS-managed secrets, [`sops`](https://github.com/getsops/sops) and [`age`](https://github.com/FiloSottile/age) on the host.

1. Clone this repo and `cd` into it.

2. **Environment:** copy the template:

   ```bash
   cp .env.example .env
   ```

   Set at least `BASE_PATH` to the repo root (forward slashes on Windows, e.g. `C:/dev/AI-toolkit`). Tokens managed by SOPS (Discord, GitHub PAT, HF, Tavily, Civitai, internal API keys, OAuth2 client ID/secret) live in `secrets/.env.sops` and the file-form `secrets/*.sops` blobs — not in `.env`.

3. **First-time SOPS setup** (skip if you've already done it on this machine — see [secrets runbook](docs/runbooks/secrets.md)):

   ```bash
   age-keygen -o ~/.config/sops/age/keys.txt
   chmod 600 ~/.config/sops/age/keys.txt
   # Back up the AGE-SECRET-KEY-1... line to a password manager.
   # Paste the matching age1... public key into secrets/.sops.yaml.
   ```

4. **Bring up the stack:**

   ```bash
   make up         # decrypts secrets/*.sops to ~/.ai-toolkit/runtime/, then docker compose up -d
   ```

   Or, on Windows / without `make`:

   ```powershell
   ./scripts/secrets/decrypt.sh
   .\compose.ps1 up -d
   ```

   The `compose` / `compose.ps1` wrapper runs hardware detection before each command; it regenerates `overrides/compute.yml` to match the GPU it sees.

5. **Sign in:** open `https://<your-tailnet-host>.<tailnet>.ts.net/` over Tailscale. Caddy redirects to `/dash/`, which goes through Google SSO and lands you on the dashboard. The other UIs are reached directly on the tailnet at their host ports (Open WebUI `:3000`, n8n `:5678`, ComfyUI `:8188`, Hermes `:9119`) — they're SPAs that emit absolute `/assets/` URLs and can't be cleanly sub-path-mounted under a single host.

**Lighter bring-up** (no auto hardware detection — you've already pinned `COMPUTE_MODE` and `COMPOSE_FILE`):

```bash
docker compose --env-file ~/.ai-toolkit/runtime/.env up -d
```

## Configuration

Primary references: **[`.env.example`](.env.example)** for plaintext env, and **[`secrets/.env.sops`](secrets/.env.sops)** (decrypt with `sops`) for managed tokens.

| Area | Variables (examples) |
|------|----------------------|
| Paths | `BASE_PATH`, `DATA_PATH` |
| Models | `LLAMACPP_MODEL`, `DEFAULT_MODEL`, `EMBED_MODEL` |
| SSO front door | `CADDY_TAILNET_HOSTNAME`, `CADDY_TAILNET_DOMAIN`, `CADDY_BIND`, `OAUTH2_PROXY_CLIENT_ID/SECRET/COOKIE_SECRET` (last three live in `secrets/.env.sops`) |
| Internal API auth | `LITELLM_MASTER_KEY`, `DASHBOARD_AUTH_TOKEN`, `OPS_CONTROLLER_TOKEN` (all in `secrets/.env.sops`) |
| Hermes / Discord | `HERMES_DASHBOARD_PORT`, `DISCORD_ALLOWED_USERS`, `DISCORD_REQUIRE_MENTION` (token via Docker secret) |
| MCP | `MCP_GATEWAY_SERVERS`, `HF_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, `TAVILY_API_KEY` (last three via Docker secret) |
| TurboQuant KV | `LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION`, `LLAMACPP_KV_CACHE_TYPE_K/V`, `LLAMACPP_FLASH_ATTN` |
| Compute | `COMPUTE_MODE`, `COMPOSE_FILE` (Linux/macOS uses `:` separator; Windows uses `;`) |
| RAG profile | `EMBED_MODEL`, `QDRANT_PORT`, `RAG_COLLECTION`, … |

Auto-generated and gitignored: `overrides/compute.yml` (from hardware detection), `auth/caddy/certs/*` (from `tailscale cert`), `~/.ai-toolkit/runtime/` (decrypted secrets).

Full reference: [docs/configuration.md](docs/configuration.md).

## Usage

- **Daily restart:** `make up` (decrypts then `docker compose up -d`).
- **Log tail:** `make logs` or `docker compose logs -f --tail=100 <service>`.
- **One-off model pulls:** dashboard "Model pulls" panel, or `docker compose --profile models run --rm gguf-puller` for GGUF (`GGUF_MODELS=org/repo`).
- **RAG profile:** `docker compose --profile rag up -d`; drop files into `data/rag-input/`.
- **MCP clients:** connect to `http://localhost:8811/mcp` (see [mcp/README.md](mcp/README.md)).
- **Rotate internal tokens:** `make rotate-internal-tokens` (regenerates `LITELLM_MASTER_KEY`, `DASHBOARD_AUTH_TOKEN`, `OPS_CONTROLLER_TOKEN`, `OAUTH2_PROXY_COOKIE_SECRET` and re-encrypts `secrets/.env.sops`).

### Architecture

```
                        Tailscale (network gate)
                                 │
                    ┌────────────┴────────────┐
                    │                         │
   Caddy (HTTPS) + oauth2-proxy        Direct host ports
   (CADDY_BIND : 443)                  (Open WebUI :3000,
        │                               n8n :5678,
        │   forward_auth → Google SSO   ComfyUI :8188,
        │   allowlist: emails.txt       Hermes :9119,
        │                               MCP :8811,
        ▼                               Qdrant :6333)
   /dash/ → dashboard:8080
   /api/* → dashboard:8080
                                 │
   ┌─────────────────────────────┼─────────────────────────────┐
   │                             │                             │
   ▼                             ▼                             ▼
Model Gateway (:11435)    MCP Gateway (:8811)    Ops Controller (internal :9000)
   │ LiteLLM                shared tools            audited compose lifecycle
   ▼                                                JSONL audit log + size rotation
llama.cpp (backend-only)
   - TurboQuant KV (tbq* / tbqp*)
   - GPU offloaded
```

Ops controller and mcp-gateway both mount `/var/run/docker.sock` (each for their own job). Hermes also mounts the socket for its built-in tools — see [docs/runbooks/bounded-hermes.md](docs/runbooks/bounded-hermes.md) for the audited HTTP API alternative and the rationale for the rollback. Details on the SSO routing pattern: [docs/runbooks/auth.md](docs/runbooks/auth.md).

### Data

Bind mounts only. Set `BASE_PATH` (and optionally `DATA_PATH`). GGUF blobs live under `models/gguf/`. See [docs/data.md](docs/data.md).

### MCP (Model Context Protocol)

[MCP Gateway](mcp/) — configure servers with `MCP_GATEWAY_SERVERS` in `.env`. Endpoint: `http://localhost:8811/mcp`. See [mcp/README.md](mcp/README.md).

### Hermes Agent

Hermes runs as two compose services (`hermes-gateway` + `hermes-dashboard`) with persistent state in the named volume `ordo-ai-stack_hermes-data` (mounted at `/home/hermes/.hermes`). Setup, upgrades, and Discord wiring: [docs/hermes-agent.md](docs/hermes-agent.md).

## Development

- Python layout: `dashboard/`, `model-gateway/`, `ops-controller/`, `rag-ingestion/`, `scripts/`, `hermes/`; Ruff config in [`pyproject.toml`](pyproject.toml).
- **Do not commit:** `.env`, `data/`, `models/`, `overrides/compute.yml`, `auth/caddy/certs/`, `mcp/.env` — see [CONTRIBUTING.md](CONTRIBUTING.md).
- **Safe to commit:** `secrets/*.sops`, `secrets/.sops.yaml` (public-key recipient).

## Testing

```bash
pip install -r tests/requirements.txt
python -m pytest tests/ -v
python -m ruff check dashboard tests model-gateway ops-controller rag-ingestion scripts comfyui-mcp orchestration-mcp worker hermes
```

**Health / diagnostics:**

```bash
./scripts/doctor.sh        # or .\scripts\doctor.ps1 on Windows
```

Optional: `DOCTOR_DEPS_TIMEOUT_SEC`; the doctor reads `DASHBOARD_AUTH_TOKEN` from `~/.ai-toolkit/runtime/.env` when probing the dashboard via bearer.

**Smoke (Docker required):**

```bash
./scripts/smoke_test.sh    # or make smoke-test
```

**CI** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): TruffleHog secret scan; pytest + ruff; `docker compose config`; optional compose smoke via workflow dispatch.

## Troubleshooting

1. **Browser hits `/dash/` and gets stuck redirecting** — usually a `CADDY_TAILNET_DOMAIN` mismatch with the Tailscale cert or an expired Tailscale cert. See [auth runbook](docs/runbooks/auth.md#troubleshooting).
2. **`make up` fails with `secret "discord_token" file is not specified`** — `~/.ai-toolkit/runtime/secrets/` is empty. Run `make decrypt-secrets` (or check that `SOPS_AGE_KEY_FILE` points at your age key).
3. **llama.cpp restart-loops with `libcuda.so.1: cannot open shared object`** — `overrides/compute.yml` lost its `deploy.resources.reservations.devices` block. Re-run `python scripts/detect_hardware.py` and `docker compose up -d --force-recreate llamacpp`.
4. **No GPU** — minimal CPU-friendly subset: `make decrypt-secrets && docker compose up -d dashboard open-webui n8n` (skip llama.cpp / ComfyUI / TurboQuant).
5. **Exposing beyond Tailscale** — don't, by default. The SSO front door is sized for a single Tailscale-reachable allowlist. If you really need wider access: enable WebUI auth (`WEBUI_AUTH=True`), harden n8n, set firewall rules at the host. See [SECURITY.md](SECURITY.md).

## Roadmap

Rolling changes: [CHANGELOG.md](CHANGELOG.md).

## Contributing

See **[CONTRIBUTING.md](CONTRIBUTING.md)**. Report security issues per **[SECURITY.md](SECURITY.md)** (do not use public issues for vulnerabilities).

## License

[MIT License](LICENSE) — Copyright (c) 2026 Ordo AI Stack contributors.
