# Component: MCP Gateway (Tool Aggregation)

## Purpose

The **MCP gateway** service exposes one **MCP HTTP endpoint** (backend port **8811** in the default compose) that aggregates multiple logical servers—web search, n8n, ComfyUI workflows, orchestration helpers, etc.—so clients (OpenClaw bridge, n8n, other agents) use **one URL** and one authentication pattern.

## Key Responsibilities

- **Catalog merge** – Upstream Docker MCP gateway plus repo **`registry-custom.yaml`** (e.g. ComfyUI, orchestration) via `gateway-wrapper.sh`.
- **Dynamic ComfyUI MCP** – Spawns or connects to the **ComfyUI** service using `COMFYUI_URL` (Docker DNS name `comfyui` on the stack network).
- **Secrets injection** – API keys (Tavily, GitHub, n8n, dashboard token for orchestration) arrive via compose environment, not committed config.
- **Operational boundary** – Default compose keeps **8811** on the internal network; optional overrides expose it for external clients.

## Registry Format

**Tool registry** (`data/mcp/registry.json`):

```json
{
  "version": 1,
  "servers": {
    "duckduckgo": {
      "image": "mcp/duckduckgo",
      "description": "Web search via DuckDuckGo",
      "scopes": ["search"],
      "allow_clients": ["*"],
      "rate_limit_rpm": 60,
      "timeout_sec": 30,
      "env_schema": {}
    },
    "github-official": {
      "image": "mcp/github-official",
      "description": "GitHub issues, PRs, repos",
      "scopes": ["github"],
      "allow_clients": ["open-webui", "openclaw"],
      "env_schema": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": {"required": true, "secret": true}
      }
    },
    "filesystem": {
      "image": "mcp/filesystem",
      "description": "File access — requires FILESYSTEM_ROOT configured",
      "scopes": ["filesystem"],
      "allow_clients": [],
      "env_schema": {
        "FILESYSTEM_ROOT": {"required": true, "secret": false}
      }
    }
  }
}
```

**Note:** `allow_clients: []` disables by default. `allow_clients: ["*"]` is explicit opt-in.


## Policy API (Dashboard `/api/mcp`)

- `GET /api/mcp/servers` — enabled list merged with registry metadata + catalog
- `POST /api/mcp/add` — add tool (updates `servers.txt`)
- `POST /api/mcp/remove` — remove tool (updates `servers.txt`)
- `GET /api/mcp/health` — per-server health status: `{server: {ok: bool, checked_at: ts}}`

## Current Policy Model

- `allow_clients: ["*"]` = all clients get the tool (default for enabled tools)
- `allow_clients: []` = tool disabled in registry (requires explicit opt-in to enable)
- Per-client enforcement: **not yet implemented** — requires Docker MCP Gateway `X-Client-ID` support (M6)

## Relationship to OpenClaw

`plugins.entries["openclaw-mcp-bridge"]` in `openclaw.json` must point at **`http://mcp-gateway:8811/mcp`** (or the correct internal URL). OpenClaw discovers tools through that single bridge—**not** duplicate per-server ComfyUI URLs.

OpenClaw-specific details:
- `openclaw-mcp-bridge` plugin → `http://mcp-gateway:8811/mcp`
- Tools surface as `gateway__duckduckgo_search`, etc.
- Future per-agent policy: add `X-Client-ID: openclaw` header; gateway checks `allow_clients`
- Planned (M6): Auto-disable after 3 consecutive health failures; per-client allowlist enforcement at gateway

## Non-Goals

- **End-user identity for every MCP call** – Per-client MCP auth is largely deferred to upstream / product choices.
- **Replacing n8n or ComfyUI** – The gateway invokes them; it does not own workflow authoring UIs.

## Dependencies

- **docker-compose** service **mcp-gateway** (build context `mcp/`).
- **Docker socket** (for gateway features that spawn tool containers, per upstream behavior).
- **data/mcp/servers.txt** – Comma-separated server list (e.g. `duckduckgo,n8n,tavily,comfyui,orchestration`). The `gateway-wrapper.sh` watches this file and **restarts the gateway process** when it changes, so edits cause a brief tool-discovery interruption.

## MCP Gateway Healthcheck

The healthcheck performs a full MCP session handshake:
1. `initialize` — establishes session, gets `Mcp-Session-Id`
2. `notifications/initialized` — completes handshake
3. `tools/list` — verifies tool catalog is populated (>0 tools)
4. Session termination (best-effort)

The gateway is not considered healthy until tools are actually loaded and discoverable.
