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

> **Operators start here → [`docs/operator-guide.md`](docs/operator-guide.md)** — the authoritative guide to the render engine, bring-up, and day-2 operations. (The stack runs as compose project **`ordo`**.)

## Overview

**Deployment model:** a single homelab operator running on their own hardware. Every user-facing UI sits behind the front door — Caddy is the only service that publishes host ports. Each prebuilt SPA is served on its own port, at the root it was compiled for: `${CADDY_TAILNET_HOSTNAME}:8443` (Open WebUI), `:8444` (Dashboard), `:8445` (n8n), `:8446` (ComfyUI), `:8447` (Hermes), `:8448` (codebase-memory) — plus `:443` as the front door (landing page, the one Google OAuth callback, `/llm/*` and `/mcp` API access, and n8n webhook/OAuth passthroughs). The operator brings their own Tailscale tailnet and Google OAuth client; the stack stitches them together so one Google sign-in (domain-scoped cookie) covers every port, gated by an email allowlist. Old subpath bookmarks (`/chat`, `/dash`, `/n8n`, `/comfy`, `/hermes`, `/codebase-memory`, `/grafana`) still work — Caddy 302s them to their port. See [docs/runbooks/auth.md](docs/runbooks/auth.md).

**Who it is for:** a homelab operator running local AI models on their own machine, exposed over their tailnet to a small allowlist of personal Google accounts, with strong operator-deployment discipline.

## How it works

Ordo is driven by a render engine, not by hand-edited compose files:

- **One source of truth** — `ordo.yaml` declares hardware, model, plugins, and overrides.
- **`ordo render`** turns that source (+ detected hardware + model catalog + plugin manifests) into the complete runtime config under `out/` (gitignored): `.env`, `docker-compose.yml`, agent context, `mcp-registry.yaml`, `manifest.json`, `secrets.env.example`.
- **Services run from the rendered output.** To change anything, edit the source and re-render — edits to derived files never survive, so the LLM context size, model choice, and agent context can never fall out of sync (the drift class that motivated the design).
- **GPU arbitration is a scheduler** (`ordo serve`, the `ops-controller` service): FIFO admission, co-run-when-it-fits, LRU idle-evict — a deterministic decision engine, not a reactive watchdog.
- **Plugins and agents are data manifests.** A service, MCP server, or agent is a declarative manifest the renderer composes in when its hardware needs are met; **Hermes is the default agent**. See [`agents/README.md`](agents/README.md).

Full engine reference, the plugin/agent registries, and the render-discipline runbook are in [`docs/operator-guide.md`](docs/operator-guide.md).

## Features

Caddy is the **only** service that publishes host ports — seven SSO-gated ports on `${CADDY_TAILNET_HOSTNAME}`, one root port per prebuilt SPA plus the `:443` front door. `model-gateway`, `mcp-gateway`, and `qdrant` publish no host port and are reachable only on the project network (or via `:443` — `/llm/*` and `/mcp` respectively).

- **Unified dashboard** (`:8444`, with a `/grafana/` embed for monitoring) — model lists, service links, dependency health, GPU/registry views, model pulls.
- **Model gateway** — LiteLLM OpenAI-compatible API in front of llama.cpp backends, reachable at `:443/llm/*` (Bearer auth).
- **Open WebUI** (`:8443`) — chat UI, served at its own port root.
- **ComfyUI** (`:8446`) — image/video (LTX-2) workflows; large model downloads on demand.
- **n8n** (`:8445`) — automation; the public webhook base (`N8N_WEBHOOK_URL=https://${CADDY_TAILNET_HOSTNAME}/n8n`) is unchanged and still passes through `:443`.
- **MCP gateway** — shared MCP tools for host clients and in-stack services, reachable at `:443/mcp` (Bearer auth).
- **Ops controller** — the render/scheduler control plane (no host port; token-auth).
- **Hermes** (`:8447`, served at `/hermes/` on its own port) — the default assistant agent (chat via the model gateway, tools via the MCP gateway, GPU via the scheduler).
- **codebase-memory** (`:8448`, served at `/codebase-memory/` on its own port) — shared codebase-memory MCP UI.
- **Voice / RAG / monitoring** — optional plugins (STT+TTS, Qdrant retrieval, Grafana+Prometheus+GPU exporter) that enable when the hardware supports them.

## Security

- **Front door:** Caddy + oauth2-proxy + Google SSO gates every browser-reachable UI at the network edge, across all seven ports. Email allowlist in `auth/oauth2-proxy/emails.txt` (never commit a real email). One Google sign-in (domain-scoped cookie) covers every port — no per-port re-auth, no new Google OAuth redirect URIs. See [docs/runbooks/auth.md](docs/runbooks/auth.md).
- **No host ports on services:** only Caddy publishes host ports — seven SSO-gated ports (tailnet-bound `:443`, `:8443`–`:8448`), one per prebuilt SPA plus the `:443` front door.
- **Secret management:** SOPS + age. Only encrypted `secrets/*.sops` blobs and config are committed; plaintext is decrypted **on the host only**, outside every container's reach, and never enters the repo or a log. Derived `.env` and operator secrets stay in separate files (`secrets.env`, `required: false`). Never synthesize placeholder secret values to clear an error — decrypt on the host. Full notes: [SECURITY.md](SECURITY.md) · [docs/runbooks/secrets.md](docs/runbooks/secrets.md).

## Architecture

```
Tailnet device → Caddy (TLS, ${CADDY_TAILNET_HOSTNAME}) → oauth2-proxy (Google SSO + email allowlist, one cookie for all ports)
                    │
                    ├── :443  → landing page + /oauth2 (Google callback) + /llm/* (LiteLLM, Bearer)
                    │           + /mcp (MCP gateway, Bearer) + n8n webhook/OAuth passthroughs
                    │           + 302 redirects from every legacy subpath (/chat /dash /n8n /comfy /hermes /codebase-memory /grafana)
                    ├── :8443 → Open WebUI (chat), served at its own root
                    ├── :8444 → Dashboard (+ /grafana/ embed), served at its own root
                    ├── :8445 → n8n UI, served at its own root
                    ├── :8446 → ComfyUI, served at its own root
                    ├── :8447 → Hermes (default agent), at /hermes/ on its own port
                    └── :8448 → codebase-memory, at /codebase-memory/ on its own port
                                        │
                                        ├── Model Gateway → LiteLLM → llama.cpp
                                        ├── MCP Gateway → shared tools (SearXNG, n8n, ComfyUI, …)
                                        └── Ops Controller → render + GPU scheduler (token-auth, no host port)
```

Local-first AI; operator-deployed front door. The dashboard does not mount `docker.sock`; the scheduler's process broker is hard-scoped to the stack's own containers. Details: [PRD index](docs/product%20requirements%20docs/index.md).

## Development & testing

- **Runtime:** everything runs in containers; install Docker and set `BASE_PATH` to the repo path.
- **Substrate:** the render engine is a real `ordo` command (`pip install .`; runtime dep = just PyYAML). Python **3.12+** for tests/lint.

```bash
# render-engine tests (no host Python needed)
docker run --rm -v "$PWD:/w" -w /w python:3.11-slim \
  sh -c "pip install -q -r requirements-dev.txt && PYTHONPATH=. python -m pytest -q tests/substrate"
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)): TruffleHog secret scan, pytest + ruff, and a real `docker compose config` gate on the rendered stack.

## Access & deployment models

The stack's UIs are served through a **swappable edge layer** — the same rendered compose stack
behind any of three front doors: a private **Tailscale tailnet** (the current default), a
**self-hosted public domain**, or a **cloud VM**. All three keep the same Google SSO gate; the public
ones add exposure and hardening requirements. See [docs/deployment-models.md](docs/deployment-models.md).

## Docs

[Operator guide (`docs/operator-guide.md`)](docs/operator-guide.md) · [Access & deployment models](docs/deployment-models.md) · [Auth front door](docs/runbooks/auth.md) · [Secrets](docs/runbooks/secrets.md) · [Data](docs/data.md) · [Hermes agent](agents/README.md) · [PRD index](docs/product%20requirements%20docs/index.md) · [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md)

## License

[MIT License](LICENSE) — Copyright (c) 2026 Ordo contributors.
