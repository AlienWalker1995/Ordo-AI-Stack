# Component: OpenClaw Assistant Layer

## Purpose

OpenClaw is the **assistant execution and governance layer** for the stack—not only a CLI. It covers:

- **Local CLI** (debugging, scripting, `devices list`, workflow triggers)
- **Control UI** (web UI exposed via the gateway; default host mapping in compose uses `OPENCLAW_UI_PORT` → sidecar → loopback UI inside the container)
- **Channel bots** (Discord / Telegram) when configured
- **Plugin extensions** (e.g. `openclaw-mcp-bridge` → single MCP gateway URL)
- **Workspace governance** (`MEMORY.md`, `SOUL.md`, `AGENTS.md`, `TOOLS.md`, sync scripts)

It enforces **token-based gateway auth** (`OPENCLAW_GATEWAY_TOKEN`), coordinates with **model-gateway** and **MCP**, and keeps workspace templates in sync (`ensure_openclaw_workspace` scripts).

## Key Responsibilities

1. **Gateway RPC** – `/commands` and related RPCs for devices, tools, and assistant flows; clients include the Control UI, bots, and `run-cli.ps1` (Docker profile `openclaw-cli`).
2. **Unified secrets** – Gateway token and channel bot tokens are preferred from `.env`; `merge_gateway_config.py` injects gateway auth and SecretRefs where supported.
3. **MCP integration** – One bridge URL to **mcp-gateway** (aggregated tools); avoid per-server duplicate URLs for ComfyUI vs gateway.
4. **Security posture** – Optional hardening (`OPENCLAW_SECURE`, unrestricted container flags documented in `openclaw/OPENCLAW_SECURE.md`); Control UI device auth can be relaxed in Docker via merged config.
5. **Extensibility** – Plugins under the OpenClaw config layout; stack adds MCP bridge entry via config-sync / merge scripts.

## OpenClaw Integration Map (Confirmed)

| Aspect | Current State | Config Location |
|--------|---------------|-----------------|
| **Model routing** | `models.providers.gateway` (`baseUrl: http://model-gateway:11435/v1`, `api: openai-completions`); default model configurable per deployment | `data/openclaw/openclaw.json` |
| **MCP tools** | `openclaw-mcp-bridge` plugin → `http://mcp-gateway:8811/mcp`; tools surface as `gateway__<tool>` | `data/openclaw/openclaw.json` |
| **Config sync** | `openclaw-config-sync` runs `merge_gateway_config.py` before gateway start; adds gateway provider if missing | `docker-compose.yml` |
| **Auth** | Gateway token via `OPENCLAW_GATEWAY_TOKEN` in `.env`; gateway auth mode `token` | `.env`, `openclaw.json` |
| **Service ID header** | `headers.X-Service-Name: openclaw` → dashboard shows "openclaw" in throughput | `openclaw.json` |
| **Workspace sync** | Seeds workspace `*.md` from `openclaw/workspace/` into `data/openclaw/workspace/` when missing; always refreshes `health_check.sh` and `agents/` | `docker-compose.yml` |

## Trust Model: Orchestrator / Browser Paradigm

```
┌─────────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR TIER                                              │
│  openclaw-gateway                                               │
│  • Holds all session credentials                                │
│  • Holds openclaw.json (gateway auth; channel tokens via        │
│    SecretRef + .env when configured)                            │
│  • Directs tool calls and model calls                           │
│  • Trusts tool outputs structurally, not verbatim               │
└──────────────────────────────┬──────────────────────────────────┘
                               │ gateway token only
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  BROWSER / WORKER TIER                                          │
│  openclaw-cli  (and future: openclaw-browser)                   │
│  • Holds gateway token ONLY — zero session credentials          │
│  • Workspace files: read-only                                   │
│  • Config dir (openclaw.json): NOT mounted                      │
│  • Egress to RFC1918 / metadata blocked                         │
└─────────────────────────────────────────────────────────────────┘
```

### Container Trust Tier Map

| Container | Tier | Session Credentials | openclaw.json | Workspace | Egress |
|-----------|------|---------------------|---------------|-----------|--------|
| `openclaw-gateway` | Orchestrator | All | Read-write | Read-write | Allowed (needs model-gateway, mcp) |
| `openclaw-cli` | Browser-tier | None | Not mounted | Read-only | RFC1918 blocked |
| `openclaw-browser` *(future)* | Browser-tier | None | Not mounted | Not mounted | RFC1918 + metadata blocked |

### Core Invariants

1. **No credentials in the browser tier.** A compromised worker cannot exfiltrate session tokens.
2. **Config is read-only or absent in the browser tier.** `openclaw.json` mounted only in orchestrator.
3. **Workspace is read-only in the browser tier.** Workers read; only the orchestrator writes.
4. **Egress from browser-tier containers is blocked to RFC1918 + metadata endpoints.**

## Non-Goals

- **Primary end-user chat product** – Day-to-day chat may live in Open WebUI; OpenClaw is the operational assistant and gateway.
- **Long-term canonical storage of every model transcript** – Streaming and downstream services own retention policies.

## Dependencies

- `docker compose` services: **openclaw-gateway** (and init jobs: workspace sync, config sync, plugin config), **model-gateway**, **mcp-gateway**, **dashboard**.
- **Readiness note**: `openclaw-gateway` depends on `model-gateway` and `mcp-gateway` with `condition: service_healthy` (healthcheck passes, tools loaded and discoverable). The `wait-orchestration` init container polls the dashboard readiness endpoint.
- Root **`.env`**: `OPENCLAW_GATEWAY_TOKEN`, optional `DISCORD_TOKEN` / `TELEGRAM_BOT_TOKEN`, optional `OPENCLAW_GATEWAY_INTERNAL_PORT` when using secure port overrides.
- Image: `OPENCLAW_IMAGE` (default pinned in `docker-compose.yml`).

## Typical Use

### CLI (from repo root)

```powershell
.\openclaw\scripts\run-cli.ps1 devices list
```

The script reads the token from `.env` and uses `ws://openclaw-gateway:<port>` inside the compose network (`OPENCLAW_GATEWAY_INTERNAL_PORT` when set, else `6680`).

### Re-merge config on the host (optional)

After editing `.env`, from repo root:

```powershell
python openclaw/scripts/merge_gateway_config.py
```

Loads repo `.env`, resolves `data/openclaw/openclaw.json` when the in-container default path is not present, then injects token and model list rules as documented in `openclaw/README.md`.

## Operational Note

If **`docker compose up` fails** or the gateway never becomes healthy, **no client** (CLI, UI, bots) can authenticate—fix compose dependencies and conflicts first, then re-run **openclaw-config-sync** (or `merge_gateway_config.py`) and restart **openclaw-gateway**.
