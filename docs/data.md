# Data Schemas, Lifecycle, and Persistence

> ⚠️ **V1 removed 2026-07-24 (commit 62540bf); the repo was then flattened 2026-07-24 (commit 2d4bd9c) — there is no v2; there is only Ordo.** The stack keeps a **single data root** at `C:\dev\ordo-ai-stack\data`; `site.DATA_PATH` in `ordo.yaml` renders it into the compose bind mounts. The per-directory schemas / bind-mount / backup guidance below still broadly describes what lives under `data/`, but config and bring-up flow through the render substrate: edit the declarative source `ordo.yaml` (tracked template `ordo.example.yaml`), run `ordo render` (`python -m ordo.cli render --out out`), then bring up the rendered compose from `out/` (`docker compose -p ordo … up`). Never hand-edit rendered output. Authoritative guide: [`operator-guide.md`](operator-guide.md).

Reference for where data lives, how it moves, and what survives a restart / rebuild.

## Data Sources and Sinks

### Sources

| Source | Description | Consumer |
|---|---|---|
| `out/.env` (rendered from `ordo.yaml`) | Environment configuration | All services at startup |
| `data/mcp/servers.txt` | Enabled MCP server list (comma-separated or one-per-line) | `mcp-gateway` |
| `data/mcp/registry.json` | MCP server metadata, `allow_clients`, rate limits | `mcp-gateway`, dashboard |
| `data/mcp/registry-custom.yaml` | Custom catalog fragment (e.g. ComfyUI MCP) | `mcp-gateway` |
| `data/rag-input/` | Drop zone for RAG documents | `rag-ingestion` watch directory |
| `models/gguf/` | llama.cpp GGUF files | `llamacpp` / `llamacpp-embed` bind mount |
| `models/comfyui/` | ComfyUI checkpoints, LoRAs, VAEs, encoders | `comfyui` bind mount |

### Sinks

| Sink | Description | Format |
|---|---|---|
| `data/ops-controller/audit.log` | Privileged-action audit log | JSONL (append-only) |
| `data/qdrant/` | Vector DB storage (RAG profile) | Qdrant native |
| `data/dashboard/` | Throughput samples, benchmarks, job tracking | JSON |
| `data/hermes/` | Hermes agent state (sessions, allowlists) | JSON / SQLite |
| `data/comfyui-storage/` | Generated media, custom nodes, runtime configs | mixed |
| `data/n8n-data/` | n8n workflows and credentials | n8n native |

## Data Schemas

### Audit Log

**Location:** `data/ops-controller/audit.log`. Append-only JSONL.

```json
{"timestamp":"2026-03-22T10:00:00Z","action":"model_pulled","model":"qwen3:8b","status":"success"}
{"timestamp":"2026-03-22T10:01:00Z","action":"service_started","service":"llamacpp","status":"success"}
```

| Field | Type | Description |
|---|---|---|
| `timestamp` | ISO 8601 | Event timestamp |
| `action` | string | `model_pulled`, `service_started`, `env_set`, etc. |
| `status` | string | `success`, `failed`, ... |
| `model` / `service` / `component` | string (optional) | Action-specific target |

Size-bounded: `ops-controller` rotates to `audit.log.1` when `AUDIT_LOG_MAX_BYTES` (default 10 MB) is exceeded.

### MCP Registry

**Location:** `data/mcp/registry.json`. JSON, one entry per MCP server.

```json
{
  "version": 1,
  "servers": {
    "duckduckgo": {
      "image": "mcp/duckduckgo",
      "scopes": ["search"],
      "allow_clients": ["*"],
      "rate_limit_rpm": 60,
      "timeout_sec": 30,
      "env_schema": {}
    }
  }
}
```

| Field | Type | Description |
|---|---|---|
| `allow_clients` | string[] | `["*"]` = all clients; `[]` = disabled by policy |
| `rate_limit_rpm` | int | Per-client rate limit (informational today) |
| `env_schema` | object | Required secrets (surfaced in dashboard as "needs key") |

### RAG Chunk (Qdrant Point)

Stored in Qdrant under `data/qdrant/`. Collection name defaults to `documents` (`RAG_COLLECTION`).

```json
{
  "id": "unique-chunk-id",
  "vector": [0.1, 0.2, "..."],
  "payload": {
    "document_name": "example.md",
    "chunk_index": 0,
    "content": "The actual chunk text",
    "chunk_size": 400,
    "chunk_overlap": 50
  }
}
```

Configuration: `EMBED_MODEL`, `RAG_CHUNK_SIZE`, `RAG_CHUNK_OVERLAP` in `out/.env` (rendered from `ordo.yaml`).

## Data Lifecycle

### Initialization

Triggered by `ordo render` + first `docker compose -p ordo … up` from `out/`.

- Creates `data/` and `models/` subdirectories.
- Copies the MCP registry template into `data/mcp/` if missing.
- Hardware detection (`hardware: auto` / `ordo detect`) and GPU pinning happen at render time, not via a separate script — `ordo render` inspects the host and writes the resolved config directly into `out/` (`.env`, `docker-compose.yml`); there is no `overrides/compute.yml` step to run.

All directories created this way persist across restarts and rebuilds.

### Model Pull

**llama.cpp GGUF:** `ordo fetch` (checksum-mandatory) downloads catalog models into `models/gguf/`; the dashboard's model-pull UI drives the same path. (The V1 `gguf-puller` compose service was not ported — its old endpoints return 501.)

**ComfyUI:** the dashboard's ComfyUI model-pack UI (backed by `scripts/comfyui/pull_comfyui_models.py`) downloads packs into `models/comfyui/`. First run can be tens of GB. (The V1 `comfyui-model-puller` compose service was not ported — its old endpoints return 501.)

### RAG Ingestion (`--profile rag`)

1. `rag-ingestion` watches `data/rag-input/` for new files.
2. Each file is chunked per `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP`.
3. Chunks are embedded via `EMBED_MODEL` through the model gateway.
4. Points are written to Qdrant (`data/qdrant/`).

Status: `GET /api/rag/status` on the dashboard returns current collection point count.

### Audit Logging

Every privileged call through `ops-controller` appends one JSONL line to `data/ops-controller/audit.log`, with `X-Request-ID` propagated from the dashboard. Rotation by size; export by `scp data/ops-controller/audit.log*`.

### Hermes Runtime State

Hermes maintains its own state under `data/hermes/` — session records, Discord per-user allowlists, scheduled tasks. The compose entrypoint re-seeds Docker-network endpoints on each start, so switching Docker networks doesn't require wiping state. See [hermes-agent.md](hermes-agent.md) for upgrade notes.

## Data Persistence Rules

### Persistent (bind-mounted)

| Directory | Purpose | Survives restart | Survives rebuild |
|---|---|---|---|
| `data/hermes/` | Hermes sessions + allowlists | yes | yes |
| `data/qdrant/` | Vector DB | yes | yes |
| `data/rag-input/` | RAG drop zone | yes | yes |
| `data/ops-controller/` | Audit log | yes | yes |
| `data/mcp/` | MCP config | yes | yes |
| `data/dashboard/` | Throughput / benchmarks | yes | yes |
| `data/comfyui-storage/` | ComfyUI outputs + custom nodes | yes | yes |
| `data/n8n-data/` | n8n workflows | yes | yes |
| `models/gguf/` | llama.cpp GGUF files | yes | yes |
| `models/comfyui/` | ComfyUI weights | yes | yes |

### Ephemeral

| Location | Purpose | Survives restart |
|---|---|---|
| `/tmp` (tmpfs) | Scratch | no |
| Container layer writes | Read-only rootfs on most custom services | no |

## Backup and Recovery

### What to back up

1. `data/hermes/` — agent state
2. `models/gguf/`, `models/comfyui/` — expensive to re-download
3. `data/ops-controller/audit.log*` — audit history
4. `data/qdrant/` — RAG collection
5. `ordo.yaml` and `out/secrets.env` — declarative source + operator secrets (**do not commit**)

### Full backup

```bash
tar -czf ordo-ai-stack-backup-$(date +%Y%m%d).tar.gz data/ models/ ordo.yaml out/secrets.env
```

### Selective backup (skip models, which are reproducible)

```bash
tar -czf ordo-ai-stack-state-$(date +%Y%m%d).tar.gz \
  data/hermes/ data/ops-controller/ data/qdrant/ data/mcp/ ordo.yaml out/secrets.env
```

### Restore

```bash
cd out && docker compose -p ordo down
tar -xzf ordo-ai-stack-backup-<date>.tar.gz
cd out && docker compose -p ordo up -d
```

## Data Migration

### Move `data/` to a different disk

```yaml
# ordo.yaml
site:
  DATA_PATH: /new/path/to/data
```

```bash
mkdir -p /new/path/to/data
cp -a data/. /new/path/to/data/
python -m ordo.cli render --out out
cd out && docker compose -p ordo down
cd out && docker compose -p ordo up -d
```

## Data Cleanup

| Data | Action | Frequency |
|---|---|---|
| `data/ops-controller/audit.log` | Archive rotated files (`audit.log.1` etc.) | Monthly |
| `data/rag-input/` | Remove processed files | As needed |
| `data/comfyui-storage/output/` | Prune old outputs | As needed |
| `models/gguf/` | Remove unused models | Quarterly |

```bash
# Archive current audit log
mv data/ops-controller/audit.log data/ops-controller/audit.log.$(date +%Y%m%d)

# Prune GGUF models (delete unused GGUF files)
ls models/gguf/
rm models/gguf/<model-file>.gguf
```
