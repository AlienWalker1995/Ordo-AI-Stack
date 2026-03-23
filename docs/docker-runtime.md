# Docker Runtime: Core Workspace + Volumes

This document describes the Dockerized runtime architecture, core workspace layout, volume mounts, and image build details for the ai-toolkit.

## Core Workspace Definition

### Container Working Directory

Most custom services use `/app` as their working directory:
- `dashboard/`: `WORKDIR /app`
- `model-gateway/`: `WORKDIR /app`
- `ops-controller/`: `WORKDIR /app`
- `rag-ingestion/`: `WORKDIR /app`

### OpenClaw Core Workspace

The OpenClaw agent workspace lives at `data/openclaw/workspace/` on the host and `/home/node/.openclaw/workspace/` inside the container.

| Host Path | Container Path | Description |
|---|---|---|
| `data/openclaw/workspace/SOUL.md` | `/home/node/.openclaw/workspace/SOUL.md` | Core agent identity and purpose |
| `data/openclaw/workspace/AGENTS.md` | `/home/node/.openclaw/workspace/AGENTS.md` | Agent definitions and capabilities |
| `data/openclaw/workspace/TOOLS.md` | `/home/node/.openclaw/workspace/TOOLS.md` | Tool definitions and usage |
| `data/openclaw/workspace/MEMORY.md` | `/home/node/.openclaw/workspace/MEMORY.md` | **Persistent memory** (key file for agent continuity) |
| `data/openclaw/workspace/USER.md` | `/home/node/.openclaw/workspace/USER.md` | User profile and preferences |
| `data/openclaw/workspace/IDENTITY.md` | `/home/node/.openclaw/workspace/IDENTITY.md` | Agent identity |
| `data/openclaw/workspace/HEARTBEAT.md` | `/home/node/.openclaw/workspace/HEARTBEAT.md` | Agent activity log |

These files are **persistent** — they survive container restarts because they are bind-mounted from the host.

## Volumes and Mounts Reference

### Data Volumes

| Service | Host Path | Container Path | Type | Purpose |
|---|---|---|---|---|
| ollama | `models/ollama/` | `/root/.ollama` | bind mount | Ollama model blobs (persistent) |
| dashboard | `data/openclaw/` | `/openclaw-config/` | bind mount | OpenClaw config sync |
| dashboard | `data/mcp/` | `/mcp-config/` | bind mount | MCP server config |
| dashboard | `data/dashboard/` | `/data/dashboard/` | bind mount | Throughput/benchmark data |
| dashboard | `models/comfyui/` | `/models/comfyui/` | bind mount | ComfyUI model weights |
| ops-controller | `data/ops-controller/` | `/data/` | bind mount | Audit log (append-only) |
| ops-controller | `models/comfyui/` | `/models/comfyui/` | bind mount | ComfyUI model weights |
| mcp-gateway | `data/mcp/` | `/mcp-config/` | bind mount | Server registry |
| openclaw-gateway | `data/openclaw/` | `/home/node/.openclaw/` | bind mount | Full OpenClaw workspace |
| openclaw-gateway | `data/comfyui-storage/ComfyUI/custom_nodes/` | `/home/node/.openclaw/workspace/comfyui-custom-nodes` | bind mount | Same folder as ComfyUI `custom_nodes` (agents install LTX/Juno nodes here) |
| openclaw-gateway | — | `/home/node/.openclaw/extensions/` | named volume (`openclaw-extensions`) | Plugin extensions |
| qdrant | `data/qdrant/` | `/qdrant/storage/` | bind mount | Vector DB storage |
| rag-ingestion | `data/rag-input/` | `/watch/` | bind mount | RAG input files |
| rag-ingestion | `data/qdrant/` | `/qdrant/storage/` | bind mount | Vector DB storage |
| n8n | `data/n8n-data/` | `/home/node/.n8n/` | bind mount | n8n workflow data |
| comfyui | `data/comfyui-output/` | `/root/ComfyUI/output/` | bind mount | Generated outputs |
| comfyui | `data/comfyui-storage/` | `/root/` | bind mount | ComfyUI storage (base images) |

### Docker Socket Mounts

| Service | Mount | Purpose |
|---|---|---|
| ops-controller | `/var/run/docker.sock` | Docker socket access for service lifecycle |
| mcp-gateway | `/var/run/docker.sock` | Docker socket access for MCP tooling |

### Ephemeral Storage

| Service | Mount | Type | Purpose |
|---|---|---|---|
| dashboard | `/tmp` | tmpfs | Ephemeral cache |
| model-gateway | `/tmp` | tmpfs | Ephemeral cache |
| ops-controller | `/tmp` | tmpfs | Ephemeral cache |

## Image Build Reference

### Custom Images (Local Builds)

| Service | Image Name | Base Image | Build Policy |
|---|---|---|---|
| dashboard | `ai-toolkit-dashboard:latest` | `python:3.12-slim` | `pull_policy: build` |
| model-gateway | `ai-toolkit-model-gateway:latest` | `python:3.12-slim` | `pull_policy: build` |
| ops-controller | `ai-toolkit-ops-controller:latest` | `python:3.12-slim` | `pull_policy: build` |
| mcp-gateway | `ai-toolkit-mcp-gateway:latest` | `docker/mcp-gateway:v2` | `pull_policy: build` |
| comfyui-mcp | `ai-toolkit-comfyui-mcp:latest` | `python:3.12-slim` | `pull_policy: build` |
| rag-ingestion | `ai-toolkit-rag-ingestion:latest` | `python:3.12-slim` | Built inline |

**Note:** Custom images use `:latest` tag with `pull_policy: build`, meaning they are rebuilt on `docker compose up --build`.

### External Images (Remote Registry)

| Service | Image | Registry | Tagging Strategy |
|---|---|---|---|
| ollama | `ollama/ollama:0.18.1` | Docker Hub | Semantic version |
| open-webui | `ghcr.io/open-webui/open-webui:v0.8.4` | GitHub CR | Semantic version |
| qdrant | `qdrant/qdrant:v1.13.4` | Docker Hub | Semantic version |
| openclaw-gateway | `ghcr.io/phioranex/openclaw-docker:latest` | GitHub CR | **Latest only** |
| n8n | `docker.n8n.io/n8nio/n8n:2.10.2` | Docker Hub | Semantic version |

**Important:** OpenClaw uses `:latest` only — there is no version pinning. This means updates to the OpenClaw image may include breaking changes.

## Network Architecture

### Frontend Network (Host-Accessible)

Services on `ai-toolkit-frontend` are accessible from the host:

| Service | Port | URL |
|---|---|---|
| dashboard | `8080` | `http://localhost:8080` |
| model-gateway | `11435` | `http://localhost:11435` |
| open-webui | `3000` | `http://localhost:3000` |
| comfyui | `8188` | `http://localhost:8188` |
| n8n | `5678` | `http://localhost:5678` |
| openclaw-gateway | `6680,6681,6682` | `http://localhost:6680` |
| qdrant | `6333` | `http://localhost:6333` (RAG profile) |

### Backend Network (Internal)

Services on `ai-toolkit-backend` are internal-only:

| Service | Port | Accessible From |
|---|---|---|
| ollama | `11434` | model-gateway, dashboard, other backend services |
| ops-controller | `9000` | dashboard (internal calls) |
| mcp-gateway | `8811` | MCP clients, dashboard |

### Exposing Backend Services

To expose Ollama or MCP Gateway on the host:

```bash
# Expose Ollama (port 11434)
docker compose -f docker-compose.yml -f overrides/ollama-expose.yml up -d

# Expose MCP Gateway (port 8811)
docker compose -f docker-compose.yml -f overrides/mcp-expose.yml up -d
```

## Service Dependencies

| Service | Depends On | Condition |
|---|---|---|
| model-gateway | ollama | `service_healthy` |
| model-gateway | dashboard | `service_started` |
| dashboard | ollama | `service_healthy` |
| mcp-gateway | comfyui-mcp-image | `service_completed_successfully` |
| openclaw-gateway | openclaw-workspace-sync | `service_completed_successfully` |
| openclaw-gateway | openclaw-config-sync | `service_completed_successfully` |
| openclaw-gateway | openclaw-plugin-config | `service_completed_successfully` |
| openclaw-gateway | model-gateway | `service_started` |
| openclaw-gateway | comfyui-mcp | `service_started` |
| openclaw-gateway | mcp-gateway | `service_started` |

## Security Hardening

Most custom services use Docker best practices:

- `cap_drop: [ALL]` — Drop all capabilities
- `security_opt: [no-new-privileges:true]` — Prevent privilege escalation
- `read_only: true` — Read-only container layers
- `tmpfs: ["/tmp"]` — Ephemeral tmp directory
- `logging:` with `max-size: "10m"`, `max-file: "3"` — Log rotation

## User Permissions

Custom Python services run as non-root user:

- `user: "1000:1000"` — UID/GID 1000 (appuser)
- This matches the typical user ID on Linux hosts

## Data Persistence Rules

### What Persists

✅ **All bind-mounted directories persist** across container restarts:
- `data/` — All service data, configs, audit logs
- `models/` — Model weights (Ollama, ComfyUI)
- `data/openclaw/workspace/` — Agent workspace (MEMORY.md, TOOLS.md, etc.)

### What Does Not Persist

❌ **Container-internal state is ephemeral**:
- `/tmp` — tmpfs mount, wiped on restart
- Container layer changes — read-only filesystem
- Unmounted runtime caches

## Verification Commands

### Check Core Workspace

```bash
# List OpenClaw workspace files
ls -la data/openclaw/workspace/

# Verify MEMORY.md exists and is writable
cat data/openclaw/workspace/MEMORY.md
```

### Check Volumes

```bash
# Check OpenClaw container mounts
docker compose exec openclaw-gateway mount | grep openclaw

# Check named volumes
docker volume ls | grep openclaw-extensions
```

### Check Image Build Status

```bash
# List custom images
docker images | grep ai-toolkit

# Force rebuild all custom images
docker compose build --no-cache

# Rebuild specific service
docker compose build dashboard
```

### Check Network Connectivity

```bash
# Check model-gateway can reach ollama
docker compose exec model-gateway python3 -c "import urllib.request; urllib.request.urlopen('http://ollama:11434/api/tags')"

# Check dashboard can reach model-gateway
docker compose exec dashboard python3 -c "import urllib.request; urllib.request.urlopen('http://model-gateway:11435/health')"
```

## Troubleshooting

### OpenClaw Port Confusion

**Control UI:** `http://localhost:6680/?token=<OPENCLAW_GATEWAY_TOKEN>`

**CDP Bridge:** `http://localhost:6682` — This is the browser/CDP bridge only, NOT the main UI.

### Model Gateway Not Working

If model-gateway fails to start:

```bash
# Check ollama health
docker compose exec ollama ollama list

# Restart model-gateway
docker compose restart model-gateway

# Check model-gateway logs
docker compose logs model-gateway
```

### RAG Not Working

```bash
# Check Qdrant is running
docker compose exec qdrant qdrant --version

# Check rag-ingestion logs
docker compose logs rag-ingestion

# Verify input files exist
ls -la data/rag-input/
```
