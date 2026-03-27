# MCP Module — Shared Model Context Protocol Gateway

The [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) lets AI applications connect to external tools and data. This module runs Docker's [MCP Gateway](https://github.com/docker/mcp-gateway), giving all your Ordo AI Stack services access to the same MCP servers through one endpoint.

## Layout (everything MCP lives under `mcp/`)

| Path | Role |
|------|------|
| **[gateway/](gateway/)** | Image entrypoint (`gateway-wrapper.sh`) and **template** `registry-custom.yaml` (catalog fragment: **`registry.comfyui`**). The wrapper injects secrets into **`registry-custom.docker.yaml`** and passes it as **`--additional-catalog`** (not **`--additional-registry`** — with **`--servers`**, registry files are not used for server definitions). `ensure_dirs` copies the template into **`data/mcp/`**. DuckDuckGo, n8n, and Tavily come from the online catalog; **`TAVILY_API_KEY`** is on **`mcp-gateway`** via compose. |
| **[docs/](docs/)** | MCP-specific architecture (e.g. [ComfyUI + OpenClaw](docs/comfyui-openclaw.md)). |
| **`Dockerfile`** | Builds `ordo-ai-stack-mcp-gateway` from `docker/mcp-gateway` + the wrapper above. |

**Runtime state** (not in git): **`data/mcp/`** — `servers.txt`, `registry-custom.yaml` (from template), generated `registry-custom.docker.yaml`, optional `registry.json` for policy metadata.

**OpenClaw:** Use **one** MCP URL for every catalog server — **`plugins.entries["openclaw-mcp-bridge"].config.servers.gateway.url`** → `http://mcp-gateway:8811/mcp`. Do not add separate `servers.comfyui` / per-server URLs; the Docker MCP Gateway aggregates DuckDuckGo, n8n, Tavily, ComfyUI, etc.

### Hardening + operating the full stack from OpenClaw

Security is **network + secrets + policy + host egress** — the forked bridge does not replace that. **Managing** models, nodes, workflows, and MCP servers uses **two layers**: MCP tools on **`mcp-gateway:8811`** for day‑to‑day tool calls, and **authenticated dashboard / ops-controller HTTP** (via **`DASHBOARD_AUTH_TOKEN`** / **`OPS_CONTROLLER_TOKEN`**) for privileged infra. See **[mcp/docs/openclaw-hardening-and-operations.md](docs/openclaw-hardening-and-operations.md)** for the full picture and checklists.

## Best experience: Docker Desktop MCP Toolkit

If you use **Docker Desktop 4.42+** with the [MCP Toolkit](https://docs.docker.com/ai/mcp-catalog-and-toolkit/toolkit/) enabled, you get the full Docker MCP experience:

- **Browse 200+ tools** in the catalog
- **One-click enable** — no .env editing or restarts
- **Instant availability** — tools appear immediately
- **Dynamic MCP** — agents can discover and add servers during conversations

In that case, use Docker Desktop's MCP Toolkit instead of this compose service. You can disable the mcp-gateway service in docker-compose if desired.

## Compose-based workflow (no Docker Desktop)

For Docker Engine / Docker CE without Docker Desktop, use this stack's MCP Gateway.

### Dashboard — add/remove tools (no container restart)

The dashboard at [localhost:8080](http://localhost:8080) manages MCP tools. Add or remove servers from the MCP Gateway section; changes take effect in ~10 seconds without restarting the container.

### Scripts (alternative)

```bash
# Add a tool
./scripts/mcp_add.sh fetch
./scripts/mcp_add.sh dockerhub

# Remove a tool
./scripts/mcp_remove.sh fetch
```

**Windows (PowerShell):**
```powershell
.\scripts\mcp_add.ps1 fetch
.\scripts\mcp_remove.ps1 fetch
```

The scripts update the config file and the gateway reloads automatically.

### Default servers (all orchestrated via gateway)

| Server | Purpose |
|--------|---------|
| `duckduckgo` | **Web search** — **`duckduckgo__search`** / **`gateway__duckduckgo__search`**. |
| `n8n` | Workflow automation. Set `N8N_API_KEY` in `.env` for full access. |
| `tavily` | **[Tavily](https://app.tavily.com)** — **`tavily_search`**, **`tavily_extract`**, **`tavily_crawl`**, **`tavily_map`**, **`tavily_research`**. Requires **`TAVILY_API_KEY`** in root **`.env`** (compose passes it into **`mcp-gateway`**; the catalog server **`mcp/tavily`** resolves secrets like upstream). Image: **`mcp/tavily`** ([catalog](https://hub.docker.com/mcp/server/tavily)). |
| `comfyui` | Image/audio/video via ComfyUI (custom registry). **`list_workflows`**, **`run_workflow`**, per-workflow tools, **`install_custom_node_requirements`**, **`restart_comfyui`**. OpenClaw + n8n parity: [**mcp/docs/comfyui-openclaw.md**](docs/comfyui-openclaw.md). Registry template: **`mcp/gateway/registry-custom.yaml`**; entrypoint: **`mcp/gateway/gateway-wrapper.sh`**. |

### Other catalog servers

| Server | Purpose |
|--------|---------|
| `fetch` | Fetch and parse web pages |
| `dockerhub` | Docker Hub / Docker Docs |
| `github-official` | GitHub (issues, PRs, repos) — needs `GITHUB_PERSONAL_ACCESS_TOKEN` |
| `mongodb` | MongoDB — needs connection string |
| `postgres` | PostgreSQL — needs `DATABASE_URL` |
| `filesystem` | File access — **requires a root directory**; remove if not needed. |

Servers that need API keys require extra setup (see [Secrets](#secrets)).

## Connecting Services

### Open WebUI

1. Open **Admin Settings** → **External Tools**
2. Click **+ Add Server**
3. **Type:** MCP (Streamable HTTP)
4. **Server URL:** `http://localhost:8811/mcp`
5. Save

### Cursor / Claude Desktop / VS Code

Add MCP server with URL `http://localhost:8811/mcp` (Streamable HTTP).

### N8N

Use the built-in **MCP Client Tool** node in your AI agent workflows:

1. Add an **AI Agent** (or similar) node to your workflow.
2. Add an **MCP Client Tool** sub-node to the agent.
3. Create credentials: **Transport** → HTTP Streamable.
4. **URL:** `http://mcp-gateway:8811/mcp` (use the Docker service name — n8n runs in the same network).
5. Save and run. The agent can now call tools from the MCP Gateway (web search, fetch, etc.).

See [n8n MCP Client Tool docs](https://docs.n8n.io/integrations/builtin/cluster-nodes/sub-nodes/n8n-nodes-langchain.toolmcp/).

### OpenClaw

**ComfyUI tools are not a separate MCP server.** They are registered on **`mcp-gateway:8811`** (via the `comfyui` catalog server + `registry-custom.yaml`). The OpenClaw plugin uses **one** URL — the gateway. Flat tools like **`gateway__comfyui__…`** depend on **tool discovery timing**; **`gateway__call`** always works once the gateway responds. See [**mcp/docs/comfyui-openclaw.md**](docs/comfyui-openclaw.md).

Many OpenClaw builds (e.g. 2026.2.x+ from `ghcr.io/openclaw/openclaw`) **do not accept a top-level `mcp` key** in `openclaw.json`. Use the **openclaw-mcp-bridge** plugin instead.

**Recommended — plugin (works when top-level `mcp` is rejected):**

Add to `plugins.entries` in `data/openclaw/openclaw.json`:

```json
"openclaw-mcp-bridge": {
  "enabled": true,
  "config": {
    "servers": {
      "gateway": {
        "url": "http://mcp-gateway:8811/mcp"
      }
    },
    "debug": false
  }
}
```

Ensure `"plugins": { "enabled": true, "entries": { ... } }` and restart the gateway. When using the main repo `docker compose`, the gateway runs with an entrypoint that installs `openclaw-mcp-bridge` from npm on first start (into the mounted config dir), so no custom image is needed. The plugin discovers tools from the MCP gateway and registers them as native OpenClaw tools (e.g. `gateway__duckduckgo_search`). Standalone OpenClaw: run `openclaw plugins install openclaw-mcp-bridge --pin` once, then restart. See [openclaw-mcp-bridge on npm](https://www.npmjs.com/package/openclaw-mcp-bridge).

**Alternative — top-level `mcp` (only if your OpenClaw version supports it):**

```json
{
  "mcp": {
    "servers": {
      "gateway": {
        "url": "http://mcp-gateway:8811/mcp",
        "transport": "streamable-http"
      }
    }
  }
}
```

If the gateway reports an unrecognized key or fails to start, remove the `mcp` block and use the plugin config above.

## Secrets

**Tavily:** set **`TAVILY_API_KEY`** in the **repo root** **`.env`** (same file as **`OPS_CONTROLLER_TOKEN`**). Compose passes it into **`mcp-gateway`** for the **`mcp/tavily`** catalog server. Get a key from [Tavily](https://app.tavily.com).

Other MCP servers like `github-official` need API keys. Optionally use Docker secrets:

1. Create `mcp/.env` with your keys (do **not** commit)
2. Uncomment the `secrets` block in `docker-compose.yml` for `mcp-gateway`
3. Add a `secrets` section to the compose file
4. Restart: `docker compose up -d mcp-gateway`

See [Docker MCP Gateway secrets](https://github.com/docker/mcp-gateway/tree/main/examples/secrets) for details.

## Policy (allowlist)

The file `data/mcp/registry.json` defines metadata per server, including **`allow_clients`**. An empty list means the server is disabled by policy; `["*"]` allows all clients; a list of IDs (e.g. `["dashboard", "openclaw"]`) restricts which clients can use that server. The dashboard sends `X-Client-ID: dashboard` when calling the gateway (e.g. for health checks). Future enforcement: a gateway wrapper or proxy can read `registry.json` and allow/deny requests by `X-Client-ID`; until then this is the policy source for tests and documentation.

## Requirements

- **Docker socket:** The gateway needs `/var/run/docker.sock` to spawn MCP server containers.
- **Network:** Services must be on the same Docker network to reach `http://mcp-gateway:8811`.

## Troubleshooting

- **Gateway won't start:** Ensure Docker can access the socket.
- **"Connection refused":** Use `mcp-gateway` (not `localhost`) when connecting from another container.
- **Server needs a secret:** Add the secret to `mcp/.env` and wire it via Docker secrets.
