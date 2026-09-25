# Data Schemas, Lifecycle, and Persistence

> The stack keeps a **single data root**, `data/` at the repo root; `site.DATA_PATH` in `ordo.yaml` renders it into the compose bind mounts. Config and bring-up flow through the render substrate: edit the declarative source `ordo.yaml` (tracked template `ordo.example.yaml`), run `ordo render` (`python -m ordo.cli render --out out`), then bring up the rendered stack from the repo root (`ordo up --all`). Never hand-edit rendered output. Authoritative guide: [`operator-guide.md`](operator-guide.md).

Reference for where data lives, how it moves, and what survives a restart / rebuild.

## Data Sources and Sinks

### Sources

| Source | Description | Consumer |
|---|---|---|
| `out/.env` (rendered from `ordo.yaml`) | Derived configuration | Compose interpolation (`--env-file`); each service receives only the keys it declares (`derived_env:`) |
| `out/model-gateway/mcp_servers.yaml` (rendered from `ordo.yaml`) | Enabled MCP servers, as the LiteLLM `mcp_servers` fragment | `model-gateway` (merged into its config at startup) |
| `out/mcp/servers.json` (rendered from `ordo.yaml`) | Enabled MCP servers plus the registered-plugin map | dashboard (`MCP_SERVERS_PATH=/mcp-config/servers.json`) |
| `out/model-gateway/keys.json` (rendered from the manifests' `litellm_key:` blocks) | Per-consumer virtual keys plus their model and MCP grants | `model-gateway-keys` (one-shot bootstrap) |
| `data/rag-input/` | Drop zone for RAG documents | `rag-ingestion` watch directory |
| `models/gguf/` | Optional host copy of GGUFs (`ordo fetch --models-dir models/gguf`, the native path) | Not mounted by any service |
| `models-gguf` named volume | llama.cpp GGUF files at runtime (ext4 inside the Docker VM) | `llamacpp` / `llamacpp-cpu` / `llamacpp-embed` (`/models:ro`), dashboard (`/gguf-models`, lists and deletes model files) |
| `comfyui-models` named volume | ComfyUI checkpoints, LoRAs, VAEs, encoders | `comfyui` (RO), `ops-controller` (RW, `/models/comfyui`: downloads), dashboard (RW, `/models`: lists and deletes) |

### Sinks

| Sink | Description | Format |
|---|---|---|
| `data/ops-controller/audit.log` | Privileged-action audit log | JSONL (append-only) |
| `data/ops-controller/scheduler-state.json` | GPU scheduler lease and eviction state (running leases with wall-clock deadlines, queue, evicted residents), rewritten atomically on every transition and adopted by the next ops-controller at startup (`SCHEDULER_STATE_PATH`) | JSON (versioned) |
| `qdrant-data` named volume | Vector DB storage (RAG profile) | Qdrant native |
| `data/dashboard/` | Throughput samples, benchmarks, job tracking | JSON |
| `hermes-home` named volume | Hermes agent brain (sessions, config, skills, cron) | JSON / SQLite / YAML |
| `data/comfyui-output/` | Generated media (renders) | mixed |
| `comfyui-app` named volume | ComfyUI app tree + custom nodes — app version pinned by `COMFYUI_APP_REF`, reconciled on boot ([configuration.md](configuration.md#comfyui-application-version)) | mixed |
| `n8n-data` named volume | n8n workflows and credentials | n8n native |
| `couchdb-data` named volume | CouchDB (Obsidian LiveSync) | CouchDB native |
| `open-webui-data` named volume | Open WebUI accounts + uploads | SQLite / files |

## Data Schemas

### Audit Log

**Location:** `data/ops-controller/audit.log` (`AUDIT_LOG_PATH=/data/audit.log` in the container). Append-only JSONL, one fsync'd line per record.

Every state-changing call to ops-controller (every `POST`, whatever the route) leaves exactly one record, whatever its outcome: success, dry run, refusal (`401` without the bearer, `409` during a GPU lease, `400` without `confirm`) or failure. Reads (`GET`) leave none. The records are written in one place, `ControlPlane.handle()` in `ordo/control/api.py`, so a new route is audited without opting in.

```json
{"ts":1790281806.1,"caller":"dashboard","action":"restart","target":"n8n","result":"ok","method":"POST","path":"/services/n8n/restart","status":200,"dry_run":false,"confirm":true}
{"ts":1790281810.4,"caller":"hermes","action":"start","target":"llamacpp","result":"refused","method":"POST","path":"/services/llamacpp/start","status":409,"dry_run":false,"confirm":true,"error":"'llamacpp' is evicted for a GPU lease held by ['gate-comfyui']; ..."}
{"ts":1790281815.0,"caller":"gpu-gate","action":"lease.request","target":"gate-comfyui","result":"ok","method":"POST","path":"/jobs","status":200,"dry_run":false,"confirm":false,"detail":"granted"}
```

| Field | Type | Description |
|---|---|---|
| `ts` | float | Unix timestamp |
| `caller` | string | The caller's `X-Actor` header (`dashboard`, `orchestration`, `hermes`, `gpu-gate`, `comfyui-mcp`), reduced to `[A-Za-z0-9_.:@-]`, at most 64 characters; `unknown` when absent. Self-declared: every caller holds the same bearer token |
| `action` | string | `start`, `stop`, `restart`, `recreate`, `container.restart`, `compose.up`, `compose.down`, `compose.restart`, `model_config`, `apply`, `plugin.enable`, `plugin.disable`, `lease.request`, `lease.heartbeat`, `lease.release`, `models.download`, `comfyui_pip_install`, `gpu_assign`; `unknown` for a path that is no route |
| `target` | string | The service, container, plugin, model id, lease id or file the call names (empty for a whole-stack compose verb) |
| `result` | string | `ok` (2xx/3xx), `refused` (4xx) or `error` (5xx) |
| `method`, `path`, `status` | string, string, int | The request line and the HTTP status answered |
| `dry_run`, `confirm` | bool | The request body's flags |
| `error` | string (optional) | The answer's error message, at most 300 characters |
| `detail` | string (optional) | For `lease.request`: `granted`, `queued` or `rejected` (a job the card can never hold) |

Nothing else from the request is recorded: no headers, no credential, and no body field beyond the one that names the target (a download URL's query string is dropped). Older records, from before every call was audited, have only `ts`, `caller`, `action`, `target`, `result` (and sometimes `detail`, `metadata`), and still read back.

Size-bounded: when the live file reaches 10 MB it becomes `audit.1.log`, older generations shift up (`audit.2.log` ...), and five are kept, so the log never exceeds about 60 MB. `GET /audit?limit=N` (bearer-protected, `1 <= N <= 1000`) returns the newest `N` records across the generations, newest first; the dashboard's activity feed reads it.

### MCP Registry

**Location:** `out/mcp/servers.json` (dashboard view) and `out/model-gateway/mcp_servers.yaml`
(the LiteLLM fragment), both emitted by `ordo render` from the `kind: mcp` plugin manifests.
Nothing lives under `data/mcp/` any more.

A `servers.json` entry, for the `qdrant-rag` server (service `mcp-qdrant-rag`):

```json
{
  "server_id": "qdrant-rag",
  "litellm_name": "qdrant_rag",
  "plugin_id": "qdrant-rag",
  "service": "mcp-qdrant-rag",
  "url": "http://mcp-qdrant-rag:9000/mcp",
  "tools": ["qdrant_status", "..."],
  "network": "stack"
}
```

The matching `mcp_servers.yaml` entry, keyed by `litellm_name` (hyphens replaced by underscores):

```yaml
mcp_servers:
  qdrant_rag:
    server_id: qdrant-rag
    url: http://mcp-qdrant-rag:9000/mcp
    transport: http
    available_on_public_internet: false
    timeout: 60
    mcp_info: {server_name: qdrant-rag}
```

Per-key scoping replaces the old per-client policy fields: a virtual key's
`object_permission.mcp_servers` grant decides which servers it sees, enforced by
`require_key_mcp_access_defined: true`.

LiteLLM namespaces tools `<litellm_name>-<tool>`, so Hermes sees
`gateway__memory_vault-read_note`.

### RAG Chunk (Qdrant Point)

Stored in Qdrant on the `qdrant-data` named volume. Collection name defaults to `documents` (`RAG_COLLECTION`).

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

Triggered by `ordo render` + the first `ordo up --all`.

- Creates `data/` and `models/` subdirectories.
- Emits the MCP artifacts into `out/` (`out/mcp/servers.json`, `out/model-gateway/mcp_servers.yaml`); nothing is written under `data/mcp/`.
- Hardware detection (`hardware: auto` / `ordo detect`) and GPU pinning happen at render time, not via a separate script — `ordo render` inspects the host and writes the resolved config directly into `out/` (`.env`, `docker-compose.yml`); there is no `overrides/compute.yml` step to run.

All directories created this way persist across restarts and rebuilds.

### Model Pull

**llama.cpp GGUF:** runtime models live in the `models-gguf` **named volume** (ext4 inside the Docker VM; Windows bind mounts ride the 9p bridge, which wedges under a 20GB+ sequential model load). Files get there by download, straight into the volume:

- `ordo up` fetches every model file the services it starts load that the volume lacks: the chat model and its vision projector (`llamacpp`), the CPU fallback (`llamacpp-cpu`) and the embedder (`llamacpp-embed`). `--no-fetch` skips it. Its host preflight first checks there is disk for every one of those files not already in the volume.
- `ordo fetch` does the same for the whole rendered stack and also re-verifies files already present (`ordo fetch <catalog-id>` for one entry plus its projector, `--all` for the catalog, `--plan-only` to list present/missing).

Both run a short-lived helper container (`curlimages/curl`, digest-pinned in `ordo/render/models_volume.py`) that mounts only the volume, downloads with resume into a hidden `.<file>.part`, verifies the catalog sha256 and renames the file into place. A checksum mismatch deletes the download and fails; an interrupted download resumes on the next run. Sources and checksums come from `catalog/models.yaml` (`models:` for chat, `support_models:` for the CPU fallback and embedder); an unpinned entry is refused unless `ordo fetch --allow-unverified`. A catalog entry marked `gated: true` gets `HF_TOKEN` from `out/secrets.env`, passed to the helper by name, never printed.

A vision projector is pinned in the model's `mmproj:` mapping (`file`, `source`, `sha256`, `size_bytes`) and fetched like the weights, under its model-specific `file` name. A model whose `mmproj:` is a bare path has no pinned source: without that file `llamacpp` starts with vision off; copy it in by hand: `docker run --rm -v ordo_models-gguf:/dst -v "$(pwd)/models/gguf:/src:ro" alpine cp /src/<file>.gguf /dst/`.

A model switch (the dashboard's Models page, or `POST /model-config` on `ops-controller`) never recreates `llamacpp` onto a file the volume lacks: it answers 409 naming the missing files and the `ordo fetch <catalog-id>` to run on the host, and changes nothing.

**ComfyUI:** add a model with the `download_comfyui_model` MCP tool (`url` plus `category`, e.g. `checkpoints`, `loras`, `vae`). It calls `ops-controller` `POST /models/download`, which writes into the `comfyui-models` **named volume** (the same volume ComfyUI reads RO), so the file lands where ComfyUI looks with no copy step. One download runs at a time; poll `get_comfyui_model_download_status`. Music3 weight URLs are listed under `weights:` in `services/song-gen/plugin.yaml`.

### RAG Ingestion (`--profile rag`)

1. `rag-ingestion` watches `data/rag-input/` for new files.
2. Each file is chunked per `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP`.
3. Chunks are embedded via `EMBED_MODEL` through the model gateway.
4. Points are written to Qdrant (`qdrant-data` named volume).

Status: the dashboard's `GET /api/overview` reports the collection point count as `knowledge.documents`.

### Audit Logging

`ops-controller` appends one JSONL line to `data/ops-controller/audit.log` for every state-changing control-plane call, refusals included (schema: [Audit Log](#audit-log)). Rotation by size (10 MB, five generations kept); export by copying `data/ops-controller/audit*.log`.

### Hermes Runtime State

Hermes keeps its own state in the `hermes-home` named volume (mounted at `/home/hermes/.hermes`): session records, Discord per-user allowlists, scheduled tasks. The compose entrypoint re-seeds Docker-network endpoints on each start, so switching Docker networks doesn't require wiping state. See [hermes-agent.md](hermes-agent.md) for upgrade notes.

## Data Persistence Rules

### Persistent (bind-mounted)

| Store | Purpose | Survives restart | Survives rebuild |
|---|---|---|---|
| `hermes-home` volume | Hermes brain (sessions, config, skills, cron) | yes | yes |
| `qdrant-data` volume | Vector DB | yes | yes |
| `couchdb-data` volume | CouchDB (LiveSync) | yes | yes |
| `n8n-data` volume | n8n workflows | yes | yes |
| `open-webui-data` volume | Open WebUI accounts | yes | yes |
| `models-gguf` volume | llama.cpp GGUF weights | yes | yes |
| `comfyui-models` volume | ComfyUI weights | yes | yes |
| `comfyui-app` volume | ComfyUI app + custom nodes | yes | yes |
| `data/rag-input/` | RAG drop zone | yes | yes |
| `data/n8n-files/` | n8n file exchange | yes | yes |
| `data/ops-controller/` | Audit log | yes | yes |
| `litellm-db-data` volume | LiteLLM Postgres (virtual keys, teams, spend) | yes | yes |
| `data/dashboard/` | Throughput / benchmarks | yes | yes |
| `data/comfyui-output/` | Render outputs | yes | yes |

### Ephemeral

| Location | Purpose | Survives restart |
|---|---|---|
| `/tmp` (tmpfs) | Scratch | no |
| Container layer writes | Read-only rootfs on most custom services | no |

## Backup and Recovery

### What to back up

1. `hermes-home` volume — agent brain (state, config, skills, cron)
2. `qdrant-data`, `couchdb-data`, `n8n-data`, `open-webui-data` volumes — service state
3. `data/ops-controller/audit*.log` — audit history
4. `ordo.yaml` and `out/secrets.env` — declarative source + operator secrets (**do not commit**)
5. Model volumes (`models-gguf`, `comfyui-models`) are usually skipped — weights are
   re-downloadable (`ordo fetch` / `download_comfyui_model`), just expensive.

### Host-side dirs

```bash
tar -czf ordo-ai-stack-host-$(date +%Y%m%d).tar.gz \
  data/ops-controller/ data/dashboard/ ordo.yaml out/secrets.env
```

### Named volumes (state lives on ext4 inside the Docker VM — back up via a helper container)

```bash
for v in hermes-home qdrant-data couchdb-data n8n-data open-webui-data litellm-db-data; do
  docker run --rm -v ordo_$v:/src:ro -v "$(pwd)/backups:/backup" alpine \
    tar -czf /backup/$v-$(date +%Y%m%d).tar.gz -C /src .
done
```

### Restore

```bash
(cd out && docker compose -p ordo down)
tar -xzf ordo-ai-stack-backup-<date>.tar.gz
ordo up --all
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
(cd out && docker compose -p ordo down)
ordo up --all
```

## Data Cleanup

| Data | Action | Frequency |
|---|---|---|
| `data/ops-controller/audit.log` | Optional: archive rotated files (`audit.1.log` ... `audit.5.log`) before they age out; rotation bounds the size on its own | As needed |
| `data/rag-input/` | Remove processed files | As needed |
| `data/comfyui-storage/output/` | Prune old outputs | As needed |
| `models-gguf` volume | Remove unused models | Quarterly |

```bash
# Archive the rotated audit generations (the live audit.log stays in place)
mkdir -p audit-archive && cp data/ops-controller/audit.*.log audit-archive/

# Prune GGUF models (list, then delete unused files inside the volume)
docker run --rm -v ordo_models-gguf:/models alpine ls -la /models
docker run --rm -v ordo_models-gguf:/models alpine rm /models/<model-file>.gguf
```
