# Component: Dashboard UI

## Purpose
A web-based control plane that provides a single pane of glass for:
- Managing Docker-Compose services (start/stop/restart, logs)
- Pulling and configuring AI models (Ollama, vLLM, etc.)
- Viewing workspace and memory state (MEMORY.md, SOUL.md)
- Interacting with the OpenClaw assistant layer via its Control UI
- Executing MCP tool calls from any browser (via the MCP Gateway)

## API Reference

**Base URL:** `http://dashboard:8080` (`:8080` host port)

**Auth:** Bearer token (`DASHBOARD_AUTH_TOKEN`) on all `/api/*` except health, auth/config, hardware, rag/status.

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/health` | GET | None | Dashboard + upstream service health |
| `/api/hardware` | GET | None | Host hardware stats (CPU, memory, GPU via nvidia-smi) |
| `/api/auth/config` | GET | None | Auth method in use |
| `/api/rag/status` | GET | None | Qdrant collection status + point count |
| `/api/ollama/models` | GET | Y | Installed Ollama models |
| `/api/ollama/pull` | POST | Y | Pull model (streaming progress) |
| `/api/ollama/delete` | POST | Y | Delete Ollama model |
| `/api/ollama/library` | GET | Y | Pullable models from Ollama registry (24h cache) |
| `/api/ollama/ps` | GET | Y | Models currently loaded in Ollama |
| `/api/comfyui/models` | GET | Y | Installed ComfyUI models |
| `/api/comfyui/pull` | POST | Y | Pull ComfyUI models |
| `/api/comfyui/models/{cat}/{file}` | DELETE | Y | Delete ComfyUI model |
| `/api/mcp/servers` | GET | Y | Enabled servers + registry metadata + catalog |
| `/api/mcp/add` | POST | Y | Enable MCP server |
| `/api/mcp/remove` | POST | Y | Disable MCP server |
| `/api/mcp/health` | GET | Y | Per-server health status |
| `/api/services` | GET | Y | Compose service list via ops controller |
| `/api/ops/services/{id}/start` | POST | Y | Start service |
| `/api/ops/services/{id}/stop` | POST | Y | Stop service |
| `/api/ops/services/{id}/restart` | POST | Y | Restart service |
| `/api/ops/services/{id}/logs` | GET | Y | Tail service logs |
| `/api/ops/available` | GET | Y | Check ops controller reachability |
| `/api/throughput/record` | POST | Y | Record model call (called by model-gateway) |
| `/api/throughput/stats` | GET | Y | Throughput statistics |
| `/api/throughput/service-usage` | GET | Y | Per-service model usage |
| `/api/throughput/benchmark` | POST | Y | Run token throughput benchmark |
| `/api/config/default-model` | GET | Y | Get current default model |
| `/api/config/default-model` | POST | Y | Set default model (restarts open-webui) |
| `/api/dependencies` | GET | Y | Dependency registry status |
| `/api/orchestration/readiness` | GET | Y | Orchestration readiness check |

## Core Responsibilities

- **Docker Lifecycle** – Calls the Ops Controller API (`/services/{id}/start|stop|restart`) using the `OPS_CONTROLLER_TOKEN` from `.env`. The UI never mounts `docker.sock`; it uses the controller as a secure proxy.
- **Model Management** – Lists available models, triggers `model-puller` containers, and shows pull progress. Stores model choices in the dashboard data directory (`data/dashboard/`, bind-mounted as `/data/dashboard` in the container).
- **Workspace Viewer** – Renders the Markdown workspace (`data/openclaw/workspace/`) with syntax highlighting and live editing. If bind-mount permissions break writes, use `scripts/fix_openclaw_workspace_permissions.ps1` or `scripts/fix_openclaw_workspace_permissions.sh`, then restart `openclaw-gateway`.
- **OpenClaw Integration** – Loads the Control UI (served at `/openclaw`) which uses `gateway__call` RPC to talk to the OpenClaw service. The UI forwards user intents (e.g., "list devices") to the CLI.
- **MCP Gateway Explorer** – Provides a tab to list registered MCP servers, invoke tools, and view tool output (via `gateway__call`). Uses the unified model-gateway for AI calls.

## Security Model
- All mutating UI actions require the `DASHBOARD_AUTH_TOKEN` (read from `.env`). The UI validates the token and includes it in the `Authorization: Bearer …` header for all Ops Controller calls.
- Token for model-gateway calls uses `OPENCLAW_GATEWAY_TOKEN` from `.env` (injected via compose environment).

## Non-Goals
- Direct end-user chat UI. The chat UI lives in Open WebUI; the dashboard is for *operations*.
- Storage of model weights. Models are stored in the persistent Docker volumes defined in `docker-compose.yml`.

## Dependencies
- `docker compose` (v2) installed on the host.
- `OPERATIONS` environment variables:
  - `OPS_CONTROLLER_TOKEN` – auth for the Ops Controller.
  - `OPENCLAW_GATEWAY_TOKEN` – for all model-gateway calls.
  - `DASHBOARD_AUTH_TOKEN` – for UI-to-controller auth.
- The `dashboard` service itself (Python/Flask app with static HTML frontend) runs inside the Ordo AI Stack.

## Typical Use Flow
1. Open `http://localhost:8080`.
2. Authenticate with the `DASHBOARD_AUTH_TOKEN`.
3. Use the "Services" tab to stop the `openclaw-gateway` if a token issue is suspected.
4. Navigate to the "OpenClaw" tab, click "List devices" – the UI calls `gateway__call` which runs `openclaw\scripts\run-cli.ps1 devices list` and shows the result.
5. In the "MCP" tab, add a new tool server (e.g., a custom web search provider) by clicking "Add" and filling the JSON manifest.

---

**See also:** [OpenClaw Assistant Layer](component-openclaw.md) and [Orchestration Layer](component-orchestration-layer.md).
