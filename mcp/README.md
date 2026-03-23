# MCP Module — Shared Model Context Protocol Gateway

The [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) lets AI applications connect to external tools and data. This module runs Docker's [MCP Gateway](https://github.com/docker/mcp-gateway), giving all your AI-toolkit services access to the same MCP servers through one endpoint.

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
| `n8n` | Workflow automation. Set `N8N_API_KEY` in `.env` for full access. |
| `playwright` | **Preferred browser tool** — navigate, screenshot, click, fill forms. |
| `comfyui` | Image/audio/video generation via ComfyUI (custom registry). Also exposes **`install_custom_node_requirements`** and **`restart_comfyui`** (ops-controller) when **`OPS_CONTROLLER_TOKEN`** is set — see **`mcp/registry-custom.yaml`** and **`gateway-wrapper.sh`**. |

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

Many OpenClaw builds (e.g. 2026.2.x from `ghcr.io/phioranex/openclaw-docker`) **do not accept a top-level `mcp` key** in `openclaw.json`. Use the **openclaw-mcp-bridge** plugin instead.

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

MCP servers like `github-official` need API keys. Use Docker secrets:

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
