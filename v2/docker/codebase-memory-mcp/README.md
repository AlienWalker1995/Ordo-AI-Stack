# codebase-memory-mcp (Code knowledge-graph MCP server)

V2's Codebase-Memory MCP image, referenced by the `codebase-memory` plugin as
`ordo-v2/codebase-memory-mcp:latest`. V1 builds it locally
(`ordo-ai-stack-codebase-memory-mcp:latest`) from `C:\dev\ordo-ai-stack\codebase-memory-mcp` — a
self-contained Dockerfile that downloads + sha256-verifies the pinned upstream
`DeusData/codebase-memory-mcp` portable (statically-linked) release binary and bundles the offline
`nomic-embed-code` embeddings, so it runs 100% offline (`disableNetwork: true` in the gateway
catalog). There is no public registry to digest-pin against, so it's a **project buildable image**
(pinned by build context: the Dockerfile's `CBM_VERSION` + `CBM_SHA256` args); `ordo preflight`
reports a missing one as "build first".

## Build
Build from the operator's authoritative source context (kept as the single source of truth — not
duplicated here to avoid drift), tagging the V2 image:
```
docker build -t ordo-v2/codebase-memory-mcp:latest C:/dev/ordo-ai-stack/codebase-memory-mcp
```

This image is gateway-spawned (stdio) and kept warm (`longLived: true`) so its in-memory graph
survives across calls within a session. It appears in the rendered `mcp-registry.yaml`, not as a
long-lived compose service. A fresh spawn starts with an EMPTY graph — call `index_repository`
once per session (against `/c/dev/<repo>`) before structural queries; the index also persists in
the `codebase-memory-cache` named volume.
