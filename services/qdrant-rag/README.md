# qdrant-rag-mcp (Qdrant RAG MCP server)

Build context for the Qdrant RAG MCP image, referenced by the `qdrant-rag` plugin
([`plugin.yaml`](plugin.yaml)) as `ordo/qdrant-rag-mcp`. A small Python server that embeds
queries via `llamacpp-embed` and searches the Qdrant `documents` collection. There is no public
registry to digest-pin against, so it's a **project buildable image** (pinned by build context);
`ordo preflight` reports a missing one as "build first".

## Build
```
ordo build mcp-qdrant-rag
```

This directory (`server.py`, `requirements.txt`, `Dockerfile`) is the single source of truth for
the service.

This image runs as the long-lived compose service `mcp-qdrant-rag` (rendered into
`out/model-gateway/mcp_servers.yaml` and `out/mcp/servers.json`), serving streamable HTTP on
port 9000. Its manifest sets `network: stack` because it must reach `qdrant` and `llamacpp-embed`.
