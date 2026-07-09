# MCP Module — Shared Model Context Protocol Gateway

> ⚠️ **Partially LEGACY — reconcile with v2/ (cutover 2026-07-09).** The MCP gateway is still core, but in the production **v2** stack its config-wrapper image is built from [`v2/docker/mcp-gateway/`](../v2/docker/mcp-gateway/) (`ordo-v2/mcp-gateway:latest`) and the enabled MCP servers are **rendered** into `v2/out/mcp-registry.yaml` from `kind=mcp` plugin manifests (public images digest-pinned; project images like `qdrant-rag` pinned by build context) — not hand-managed via the root `docker-compose.yml` / `mcp/` build. The tool catalog, server descriptions, connection URLs, and policy notes below remain accurate; the **build path** (`mcp/Dockerfile`, root compose) and `mcp/.env` secret flow are V1. See [`v2/PARITY.md`](../v2/PARITY.md) (MCP section) and [`docs/LEGACY-CLEANUP.md`](../docs/LEGACY-CLEANUP.md).

The [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) lets AI applications connect to external tools and data. This module runs Docker's [MCP Gateway](https://github.com/docker/mcp-gateway), giving all your Ordo AI Stack services access to the same MCP servers through one endpoint.

## Layout (everything MCP lives under `mcp/`)

| Path | Role |
|------|------|
| **[gateway/](gateway/)** | Image entrypoint (`gateway-wrapper.sh`) and **template** `registry-custom.yaml` (catalog fragment registering **`searxng`**, **`comfyui`**, **`orchestration`**, **`playwright`**, and an override of upstream **`n8n`**). The wrapper injects secrets into **`registry-custom.docker.yaml`** and passes it as **`--additional-catalog`** (not **`--additional-registry`** — with **`--servers`**, registry files are not used for server definitions). `ensure_dirs` copies the template into **`data/mcp/`**. **`duckduckgo`** comes from the online catalog; web search is served by the self-hosted **`searxng`** server (no external API key). |
| **[docs/](docs/)** | MCP-specific architecture notes. |
| **`Dockerfile`** | Builds `ordo-ai-stack-mcp-gateway` from `docker/mcp-gateway` + the wrapper above. |

**Runtime state** (not in git): **`data/mcp/`** — `servers.txt`, `registry-custom.yaml` (from template), generated `registry-custom.docker.yaml`, optional `registry.json` for policy metadata.

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

The dashboard (reached via the Caddy SSO front door at `https://${CADDY_TAILNET_HOSTNAME}/dash/` — see [docs/runbooks/auth.md](../docs/runbooks/auth.md)) manages MCP tools. Add or remove servers from the MCP Gateway section; changes take effect in ~10 seconds without restarting the container.

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
| `duckduckgo` | **Web search** — **`gateway__search`**. |
| `n8n` | Workflow automation. `N8N_API_KEY` is mounted as a Docker secret (`secrets/n8n_api_key.sops`); see [docs/runbooks/secrets.md](../docs/runbooks/secrets.md). |
| `searxng` | Private aggregated web search via the self-hosted **`searxng`** service (`services.searxng` in compose). No external API key. Replaced Tavily as the default web-research tool. |
| `comfyui` | Image/audio/video via ComfyUI (custom registry). **`list_workflows`**, **`run_workflow`**, per-workflow tools, **`install_custom_node_requirements`**, **`restart_comfyui`**. Registry template: **`mcp/gateway/registry-custom.yaml`**; entrypoint: **`mcp/gateway/gateway-wrapper.sh`**. |
| `orchestration` | Stable orchestration adapter (fixed verbs against the dashboard HTTP API, insulated from upstream gateway tool-name churn). Model-registry verbs: **`list_models`**, **`gpu_status`**, **`set_active_model`**, **`assign_model_gpu`**, **`register_model`** — Hermes and external clients use these to query and control which model runs on which GPU without directly touching `.env` or compose files. |
| `playwright` | Headless-Chromium browser automation — **`browser_navigate`**, **`browser_snapshot`**, **`browser_take_screenshot`**, **`browser_fill_form`**, network inspection, etc. Stack-pinned in **`mcp/gateway/registry-custom.yaml`** (sha-pinned image, not resolved from Docker's online catalog). ⚠️ exposes **`browser_run_code_unsafe`** (RCE-equivalent) — fine for the single trusted operator; restrict with `--caps` if exposed more widely. |

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

## Secrets

The default servers (`duckduckgo`, `n8n`, `searxng`, `comfyui`, `orchestration`, `playwright`) need no external API keys — `n8n`'s API key is supplied via Docker secrets from SOPS-encrypted material under `secrets/`.

Other MCP servers like `github-official` need API keys. Optionally use Docker secrets:

1. Create `mcp/.env` with your keys (do **not** commit)
2. Uncomment the `secrets` block in `docker-compose.yml` for `mcp-gateway`
3. Add a `secrets` section to the compose file
4. Restart: `docker compose up -d mcp-gateway`

See [Docker MCP Gateway secrets](https://github.com/docker/mcp-gateway/tree/main/examples/secrets) for details.

## Policy (allowlist)

The file `data/mcp/registry.json` defines metadata per server, including **`allow_clients`**. An empty list means the server is disabled by policy; `["*"]` allows all clients; a list of IDs (e.g. `["dashboard", "hermes"]`) restricts which clients can use that server. The dashboard sends `X-Client-ID: dashboard` when calling the gateway (e.g. for health checks). Future enforcement: a gateway wrapper or proxy can read `registry.json` and allow/deny requests by `X-Client-ID`; until then this is the policy source for tests and documentation.

## Requirements

- **Docker socket:** The gateway needs `/var/run/docker.sock` to spawn MCP server containers.
- **Network:** Services must be on the same Docker network to reach `http://mcp-gateway:8811`.

## Troubleshooting

- **Gateway won't start:** Ensure Docker can access the socket.
- **"Connection refused":** Use `mcp-gateway` (not `localhost`) when connecting from another container.
- **Server needs a secret:** Add the secret to `mcp/.env` and wire it via Docker secrets.
