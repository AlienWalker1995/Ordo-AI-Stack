# Data Schemas, Lifecycle, and Persistence

Reference for where data lives, how it moves, and what survives a restart / rebuild.

## Data Sources and Sinks

### Sources

| Source | Description | Consumer |
|---|---|---|
| `.env` | Environment configuration | All services at startup |
| `data/mcp/servers.txt` | Enabled MCP server list (comma-separated or one-per-line) | `mcp-gateway` |
| `data/mcp/registry.json` | MCP server metadata, `allow_clients`, rate limits | `mcp-gateway`, dashboard |
| `data/mcp/registry-custom.yaml` | Custom catalog fragment (e.g. ComfyUI MCP) | `mcp-gateway` |
| `data/rag-input/` | Drop zone for RAG documents | `rag-ingestion` watch directory |
| `models/gguf/` | llama.cpp GGUF files (chat + embed) | `llamacpp` / `llamacpp-embed` bind mount |
| `models/comfyui/` | ComfyUI checkpoints, LoRAs, VAEs, encoders | `comfyui` bind mount |
| `secrets/*.sops` | SOPS-encrypted env + file-form tokens | `make decrypt-secrets` → `~/.ai-toolkit/runtime/` |

### Sinks

| Sink | Description | Format |
|---|---|---|
| `data/ops-controller/audit.jsonl` | Privileged-action audit log | JSONL (append-only, rotates to `.1` at 50 MB) |
| `data/qdrant/` | Vector DB storage (RAG profile) | Qdrant native |
| `data/dashboard/` | Throughput samples, benchmarks, job tracking | JSON |
| `data/comfyui-storage/` | Generated media, custom nodes, runtime configs | mixed |
| `data/n8n-data/` | n8n workflows and credentials | n8n native |

> **Note on Hermes state.** Hermes' live runtime state lives in the named Docker volume `ordo-ai-stack_hermes-data` (mounted at `/home/hermes/.hermes`). The `data/hermes/` directory on the host is leftover from the pre-volume migration (`5bd23fd`) and is **not** mounted live — see [hermes-agent.md](hermes-agent.md).

## Data Schemas

### Audit Log

**Location:** `data/ops-controller/audit.jsonl`. Append-only JSONL, one event per privileged ops-controller call.

```json
{"ts": 1745611200.123, "caller": "hermes", "action": "container.restart", "target": "open-webui", "result": "ok"}
{"ts": 1745611205.456, "caller": "dashboard", "action": "compose.up", "target": "open-webui", "result": "ok"}
```

| Field | Type | Description |
|---|---|---|
| `ts` | float (Unix epoch seconds) | Event timestamp |
| `caller` | string | `hermes`, `dashboard`, … |
| `action` | string | `container.list`, `container.logs`, `container.restart`, `compose.up`, `compose.down`, `compose.restart` |
| `target` | string | Container name / compose service / empty for whole-stack |
| `result` | string | `ok` or short error message |

Size-bounded: `ops-controller` rotates to `audit.1.jsonl` when `AUDIT_LOG_MAX_BYTES` (default 50 MB) is exceeded. One historical generation only.

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

Configuration: `EMBED_MODEL`, `RAG_CHUNK_SIZE`, `RAG_CHUNK_OVERLAP` in `.env`.

## Data Lifecycle

### Initialization

Triggered by `compose` wrapper / `scripts/ensure_dirs.sh` / `scripts/ensure_dirs.ps1` on first bring-up.

- Creates `data/` and `models/` subdirectories.
- Copies the MCP registry template into `data/mcp/` if missing.
- Runs `scripts/detect_hardware.py` to generate `overrides/compute.yml`.

All directories created this way persist across restarts and rebuilds.

### Model Pull

**llama.cpp GGUF:** `docker compose --profile models run --rm gguf-puller` with `GGUF_MODELS=org/repo` fetches GGUF files into `models/gguf/`. The dashboard's "Model pulls" panel calls the same flow via the ops-controller `POST /models/pull` endpoint (audited).

**ComfyUI:** `docker compose run --rm comfyui-model-puller` downloads the pack defined by `COMFYUI_PACKS` (default includes LTX-2 variants) into `models/comfyui/`. First run can be tens of GB.

### RAG Ingestion (`--profile rag`)

1. `rag-ingestion` watches `data/rag-input/` for new files.
2. Each file is chunked per `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP`.
3. Chunks are embedded by `llamacpp-embed` (serving `EMBED_MODEL` from `models/gguf/`) through the model gateway.
4. Points are written to Qdrant (`data/qdrant/`).

Status: `GET /api/rag/status` on the dashboard returns current collection point count.

### Audit Logging

Every privileged call through `ops-controller` appends one JSONL line to `data/ops-controller/audit.jsonl`. Rotation by size; export by `cp data/ops-controller/audit*.jsonl <backup-dir>/`.

### Hermes Runtime State

Hermes maintains its own state inside the named Docker volume `ordo-ai-stack_hermes-data` (mounted at `/home/hermes/.hermes`) — session records, Discord per-user allowlists, scheduled tasks, FTS-indexed memories, installed skills. The compose entrypoint re-seeds the four Docker-network config keys (`model.base_url`, `model.api_key`, `model.default`, `mcp_servers.gateway.url`) on each start, so switching Docker networks doesn't require wiping state. The `data/hermes/` directory on the host is stale leftover and is not mounted into containers. See [hermes-agent.md](hermes-agent.md) for upgrade notes.

## Data Persistence Rules

### Persistent (bind-mounted)

| Location | Purpose | Survives restart | Survives rebuild |
|---|---|---|---|
| `ordo-ai-stack_hermes-data` (named volume) | Hermes sessions, memories, skills, scheduled jobs | yes | yes |
| `data/qdrant/` | Vector DB | yes | yes |
| `data/rag-input/` | RAG drop zone | yes | yes |
| `data/ops-controller/` | Audit log | yes | yes |
| `data/mcp/` | MCP config | yes | yes |
| `data/dashboard/` | Throughput / benchmarks | yes | yes |
| `data/comfyui-storage/` | ComfyUI outputs + custom nodes | yes | yes |
| `data/n8n-data/` | n8n workflows | yes | yes |
| `models/gguf/` | llama.cpp GGUF files | yes | yes |
| `models/comfyui/` | ComfyUI weights | yes | yes |
| `secrets/*.sops` | Encrypted secrets (committable) | yes | yes |

### Ephemeral

| Location | Purpose | Survives restart |
|---|---|---|
| `/tmp` (tmpfs) | Scratch | no |
| Container layer writes | Read-only rootfs on most custom services | no |

## Backup and Recovery

### What to back up

1. `ordo-ai-stack_hermes-data` named volume — agent state (sessions, memories, skills, schedules)
2. `models/gguf/`, `models/comfyui/` — expensive to re-download
3. `data/ops-controller/audit*.jsonl` — audit history
4. `data/qdrant/` — RAG collection
5. `.env` — plaintext env (**do not commit**)
6. `~/.config/sops/age/keys.txt` — age private key (back up to a password manager; without it, `secrets/*.sops` is unrecoverable)

### Full backup

```bash
docker run --rm -v ordo-ai-stack_hermes-data:/source -v "$PWD:/backup" \
  alpine tar -czf /backup/hermes-volume-$(date +%Y%m%d).tar.gz -C /source .
tar -czf ai-toolkit-backup-$(date +%Y%m%d).tar.gz data/ models/ .env secrets/
```

### Selective backup (skip models, which are reproducible)

```bash
docker run --rm -v ordo-ai-stack_hermes-data:/source -v "$PWD:/backup" \
  alpine tar -czf /backup/hermes-volume-$(date +%Y%m%d).tar.gz -C /source .
tar -czf ai-toolkit-state-$(date +%Y%m%d).tar.gz \
  data/ops-controller/ data/qdrant/ data/mcp/ data/dashboard/ .env secrets/
```

### Restore

```bash
docker compose down
tar -xzf ai-toolkit-backup-<date>.tar.gz
docker volume create ordo-ai-stack_hermes-data
docker run --rm -v ordo-ai-stack_hermes-data:/dest -v "$PWD:/backup" \
  alpine sh -c "cd /dest && tar -xzf /backup/hermes-volume-<date>.tar.gz"
make up
```

## Data Migration

### Move `data/` to a different disk

```bash
# .env
DATA_PATH=/new/path/to/data
```

```bash
mkdir -p /new/path/to/data
cp -a data/. /new/path/to/data/
docker compose down
docker compose up -d
```

## Data Cleanup

| Data | Action | Frequency |
|---|---|---|
| `data/ops-controller/audit.jsonl` | Archive `audit.1.jsonl` before next rotation | Monthly |
| `data/rag-input/` | Remove processed files | As needed |
| `data/comfyui-storage/output/` | Prune old outputs | As needed |
| `models/gguf/` | Remove unused GGUFs | Quarterly |

```bash
# Archive rotated audit log
mv data/ops-controller/audit.1.jsonl data/ops-controller/audit-$(date +%Y%m%d).jsonl

# Prune GGUF models (filesystem-level)
ls -lh models/gguf/
rm models/gguf/<unused-model>.gguf
```
