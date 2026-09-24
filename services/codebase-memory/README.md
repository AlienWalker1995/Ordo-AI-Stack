# codebase-memory-mcp

MCP server that gives Hermes a **structural code knowledge graph** of the repos under your code
root (call graphs, trace paths, architecture views, symbol search), so it can navigate code instead
of grepping blindly.

It wraps the upstream [`DeusData/codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp)
release binary (MIT). The binary is a single static executable with a **bundled, offline embedding
model** (`nomic-embed-code`): no API keys, no network at runtime.

The browsable **UI** is a separate service plugin, see
[`../codebase-memory-ui/README.md`](../codebase-memory-ui/README.md).

## Build

Project buildable image (no public registry to digest-pin against, so it is pinned by this build
context: the `Dockerfile`'s `CBM_VERSION` + `CBM_SHA256`), which is why `ordo preflight` reports a
missing one as "build first":

```
ordo build mcp-codebase-memory
```

## How it's wired

This is a **long-lived compose service**, `mcp-codebase-memory`, on the internal `ordo-mcp-net`
network. LiteLLM's MCP gateway on `model-gateway` dials it at
`http://mcp-codebase-memory:9000/mcp` and re-exports its tools to callers whose virtual key grants
this server.

- `Dockerfile` downloads + checksum-verifies the pinned release (portable/static build) and bridges
  its **stdio** server to **streamable HTTP** with a pinned `mcp-proxy`:
  ```
  CMD ["mcp-proxy", "--host", "0.0.0.0", "--port", "9000", "--stateless", "--pass-environment", "--", "codebase-memory-mcp"]
  ```
  `--stateless` is required because LiteLLM's outbound MCP client re-initialises per operation and
  carries no session id; `--pass-environment` is required because `mcp-proxy` otherwise starts the
  stdio child with an empty environment, so `CBM_*` would never reach it.
- [`plugin.yaml`](plugin.yaml) (`kind: mcp`) declares the service: `transport: http`, `port: 9000`,
  `network: internal`, and the mounts, which compose interpolates:
  - `${CODE_ROOT:-/c/dev}:/c/dev:ro` - your code root, **read-only**.
  - `codebase-memory-cache:/cache` - a **named volume** for the persistent index, read-write.
- `network: internal` is the exfiltration control: only `model-gateway` can reach this container and
  the container itself can reach nothing. No Docker socket and no host-path allowlist are involved.

`ordo render` emits the server into `out/model-gateway/mcp_servers.yaml` (the LiteLLM fragment its
entrypoint merges) and `out/mcp/servers.json` (the dashboard's server list).

## How the tools appear

LiteLLM namespaces tools `<litellm_name>-<tool>`, where `litellm_name` is the server id with
hyphens replaced by underscores. Hermes adds its own gateway prefix, so it sees
`gateway__codebase_memory-search_graph`, `gateway__codebase_memory-trace_path`, and so on.

## Enabling it

1. Set `CODE_ROOT` in `ordo.yaml`'s `site:` block to the **host** path that contains your repos,
   e.g. `CODE_ROOT: C:/dev` (must match what Hermes sees at `/c/dev`); it flows verbatim into the
   rendered `.env`.
2. Build the image (see the **Build** section above):
   `ordo build mcp-codebase-memory`
3. The `codebase-memory` plugin (co-located [`plugin.yaml`](plugin.yaml)) isn't NVIDIA-gated, so
   it's already on under the default `plugins: auto`. Run `ordo render`, then bring the stack up
   from `out/` and recreate `model-gateway` so LiteLLM reads the updated
   `out/model-gateway/mcp_servers.yaml` (MCP servers are read from the config at startup, there is
   no hot reload).

## Indexing

The index is built on demand and persists in the `codebase-memory-cache` named
volume. Hermes indexes a repo once (`index_repository` with a `/c/dev/<repo>` path),
then queries it (`search_graph`, `trace_path`, `get_architecture`, ...). Subsequent
sessions reuse the persisted index.

## Security

- Code root is mounted **read-only**; the container has **no network** beyond the gateway's inbound
  call.
- Indexing honors `.gitignore` and a project-level **`.cbmignore`** (gitignore
  syntax). This repo ships a root `.cbmignore` that excludes secrets and
  non-source paths as defense-in-depth; add one to each other indexed repo.
- Results are **navigation hints**: confirm in the actual file before editing.

## Bumping the version

Update `CBM_VERSION` + `CBM_SHA256` in the `Dockerfile` together (sha256 from the
release `checksums.txt`, `...-linux-amd64-portable.tar.gz` line), then rebuild.
