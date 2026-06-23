# codebase-memory-mcp

Gateway-spawned MCP server that gives Hermes a **structural code knowledge graph**
of the repos under your code root — call graphs, trace paths, architecture views,
symbol search — so it can navigate code instead of grepping blindly.

It wraps the upstream [`DeusData/codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp)
release binary (MIT). The binary is a single static executable with a **bundled,
offline embedding model** (`nomic-embed-code`) — no API keys, no network at runtime.

## How it's wired

This is **not** a long-lived compose service. Like the other custom MCPs in this
stack (`qdrant-rag`, `comfyui`, `orchestration`), it is an **image** that the
`mcp-gateway` spawns as a sibling container over **stdio** when the tool is called.

- `Dockerfile` — downloads + checksum-verifies the pinned release (portable/static
  build) and sets the binary as the MCP stdio entry point.
- `docker-compose.yml` → `codebase-memory-mcp-image` (build-only, profile
  `codebase-memory`) builds `ordo-ai-stack-codebase-memory-mcp:latest`.
- `mcp/gateway/registry-custom.yaml` → the `codebase-memory` catalog entry tells the
  gateway how to spawn it:
  - `volumes: ["PLACEHOLDER_CODE_ROOT:/c/dev:ro", "codebase-memory-cache:/cache"]`
    — your code root mounted **read-only**, plus a **named volume** for the
    persistent index. (`PLACEHOLDER_CODE_ROOT` is substituted from `CODE_ROOT` by
    `gateway-wrapper.sh`.)
  - `longLived: true` — keep the container warm across calls within a session.
  - `disableNetwork: true` — the indexer needs no egress; this blocks exfiltration
    of indexed code.

Because the gateway spawns siblings via the host Docker daemon, the `/c/dev` bind
source is a **host path** and (under the gateway's 2026-06 bind-mount hardening)
must be read-only and allow-listed via `MCP_GATEWAY_DOCKER_BIND_ALLOWED_PATHS`
(set to `CODE_ROOT` in compose). The cache uses a named volume, which is exempt
from those host-path restrictions and so can be read-write.

## Enabling it

1. Set `CODE_ROOT` in `.env` to the **host** path that contains your repos, e.g.
   `CODE_ROOT=C:/dev` (must match what Hermes sees at `/c/dev`).
2. Build the image (opt-in profile):
   `docker compose --profile codebase-memory build codebase-memory-mcp-image`
3. Enable the server in the gateway: `./scripts/mcp_add.sh codebase-memory`
   (the gateway hot-reloads in ~10s; no restart needed).

## Indexing

The index is built on demand and persists in the `codebase-memory-cache` named
volume. Hermes indexes a repo once (`index_repository` with a `/c/dev/<repo>` path),
then queries it (`search_graph`, `trace_path`, `get_architecture`, ...). Subsequent
sessions reuse the persisted index.

## Security

- Code root is mounted **read-only**; the container has **no network**.
- Indexing honors `.gitignore` and a project-level **`.cbmignore`** (gitignore
  syntax). This repo ships a root `.cbmignore` that excludes secrets and
  non-source paths as defense-in-depth; add one to each other indexed repo.
- Results are **navigation hints** — confirm in the actual file before editing.

## Bumping the version

Update `CBM_VERSION` + `CBM_SHA256` in the `Dockerfile` together (sha256 from the
release `checksums.txt`, `...-linux-amd64-portable.tar.gz` line), then rebuild.
