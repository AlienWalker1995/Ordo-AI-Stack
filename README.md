```
  ___          _
 / _ \ _ __ __| | ___
| | | | '__/ _` |/ _ \
| |_| | | | (_| | (_) |
 \___/|_|  \__,_|\___/

──────────────────────────────────────────────────
Local-first AI stack: LLMs, chat UI, image/video (ComfyUI), automation (n8n) — one declarative source, one dashboard.
```

**Ordo** is a local-first, single-operator AI stack. It runs llama.cpp-backed models behind an **OpenAI-compatible** LiteLLM gateway, **Open WebUI** for chat, **ComfyUI** for image/video diffusion, **n8n** for automation, and an **MCP gateway** for shared tools — all fronted by a unified **dashboard** and reached through a single **Caddy + oauth2-proxy + Tailscale + Google SSO** front door.

Its defining idea is **config-as-render**: one declarative source (`ordo.yaml`) is rendered into the running config (`.env`, `docker-compose.yml`, agent context, MCP registry). Derived files are regenerated, never hand-edited — so configuration drift is structurally impossible.

> **Operators start here → [`v2/README.md`](v2/README.md)** — the authoritative guide to the render engine, bring-up, and day-2 operations. (The stack runs as compose project **`ordo`**; its render substrate lives in the `v2/` directory.)

## Overview

**Deployment model:** a single homelab operator running on their own hardware. Every user-facing UI sits behind the front door — no UI service publishes a host port directly. The operator brings their own Tailscale tailnet and Google OAuth client; the stack stitches them together so every UI is reachable at `https://${CADDY_TAILNET_HOSTNAME}/<service>/` after one Google sign-in, gated by an email allowlist. See [docs/runbooks/auth.md](docs/runbooks/auth.md).

**Who it is for:** a homelab operator running local AI models on their own machine, exposed over their tailnet to a small allowlist of personal Google accounts, with strong operator-deployment discipline.

## How it works

Ordo is driven by a render engine, not by hand-edited compose files:

- **One source of truth** — `v2/ordo.yaml` declares hardware, model, plugins, and overrides.
- **`ordo render`** turns that source (+ detected hardware + model catalog + plugin manifests) into the complete runtime config under `v2/out/` (gitignored): `.env`, `docker-compose.yml`, agent context, `mcp-registry.yaml`, `manifest.json`, `secrets.env.example`.
- **Services run from the rendered output.** To change anything, edit the source and re-render — edits to derived files never survive, so the LLM context size, model choice, and agent context can never fall out of sync (the drift class that motivated the design).
- **GPU arbitration is a scheduler** (`ordo serve`, the `ops-controller` service): FIFO admission, co-run-when-it-fits, LRU idle-evict — a deterministic decision engine, not a reactive watchdog.
- **Plugins and agents are data manifests.** A service, MCP server, or agent is a declarative manifest the renderer composes in when its hardware needs are met; **Hermes is the default agent**. See [`v2/agents/README.md`](v2/agents/README.md).

Full engine reference, the plugin/agent registries, and the render-discipline runbook are in [`v2/README.md`](v2/README.md).

## Features

All UI ports are **internal** (container-network); operators reach them via the front door under `https://${CADDY_TAILNET_HOSTNAME}/<path>/`. The only host-published ports are Caddy `:443` (tailnet-bound) and `127.0.0.1`-bound publishes of `model-gateway`, `mcp-gateway`, and `qdrant` for host-side tools.

- **Unified dashboard** (`/dash/`) — model lists, service links, dependency health, GPU/registry views, model pulls.
- **Model gateway** — LiteLLM OpenAI-compatible API in front of llama.cpp backends.
- **Open WebUI** (`/`) — chat UI at the root of the tailnet hostname.
- **ComfyUI** (`/comfy/`) — image/video (LTX-2) workflows; large model downloads on demand.
- **n8n** (`/n8n/`) — automation.
- **MCP gateway** — shared MCP tools for host clients and in-stack services.
- **Ops controller** — the render/scheduler control plane (no host port; token-auth).
- **Hermes** (`/hermes/`) — the default assistant agent (chat via the model gateway, tools via the MCP gateway, GPU via the scheduler).
- **Voice / RAG / monitoring** — optional plugins (STT+TTS, Qdrant retrieval, Grafana+Prometheus+GPU exporter) that enable when the hardware supports them.

## Security

- **Front door:** Caddy + oauth2-proxy + Google SSO gates every browser-reachable UI at the network edge. Email allowlist in `auth/oauth2-proxy/emails.txt` (never commit a real email). See [docs/runbooks/auth.md](docs/runbooks/auth.md).
- **No host ports on services:** only the edge Caddy publishes a port (tailnet-bound `:443`).
- **Secret management:** SOPS + age. Only encrypted `secrets/*.sops` blobs and config are committed; plaintext is decrypted **on the host only**, outside every container's reach, and never enters the repo or a log. Derived `.env` and operator secrets stay in separate files (`secrets.env`, `required: false`). Never synthesize placeholder secret values to clear an error — decrypt on the host. Full notes: [SECURITY.md](SECURITY.md) · [docs/runbooks/secrets.md](docs/runbooks/secrets.md).

## Architecture

```
Tailnet device → Caddy :443 (TLS) → oauth2-proxy (Google SSO + email allowlist)
                                          │
                                          ├── /          → Open WebUI
                                          ├── /dash/     → Dashboard
                                          ├── /n8n/      → n8n
                                          ├── /comfy/    → ComfyUI
                                          └── /hermes/   → Hermes (default agent)
                                                  │
                                                  ├── Model Gateway → LiteLLM → llama.cpp
                                                  ├── MCP Gateway → shared tools (SearXNG, n8n, ComfyUI, …)
                                                  └── Ops Controller → render + GPU scheduler (token-auth, no host port)
```

Local-first AI; operator-deployed front door. The dashboard does not mount `docker.sock`; the scheduler's process broker is hard-scoped to the stack's own containers. Details: [PRD index](docs/product%20requirements%20docs/index.md).

## Development & testing

- **Runtime:** everything runs in containers; install Docker and set `BASE_PATH` to the repo path.
- **Substrate:** the render engine is a real `ordo` command (`pip install ./v2`; runtime dep = just PyYAML). Python **3.12+** for tests/lint.

```bash
# render-engine tests (no host Python needed)
docker run --rm -v "$PWD/v2:/w" -w /w python:3.11-slim \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): TruffleHog secret scan, pytest + ruff, and a real `docker compose config` gate on the rendered stack.

## Docs

[Operator guide (`v2/README.md`)](v2/README.md) · [Auth front door](docs/runbooks/auth.md) · [Secrets](docs/runbooks/secrets.md) · [Data](docs/data.md) · [Hermes agent](v2/agents/README.md) · [PRD index](docs/product%20requirements%20docs/index.md) · [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md)

## License

[MIT License](LICENSE) — Copyright (c) 2026 Ordo contributors.
