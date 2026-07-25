# qdrant-rag-mcp (Qdrant RAG MCP server)

Build context for the Qdrant RAG MCP image, referenced by the `qdrant-rag` plugin
([`plugin.yaml`](plugin.yaml)) as `ordo/qdrant-rag-mcp:latest`. A small Python server that embeds
queries via `llamacpp-embed` and searches the Qdrant `documents` collection. There is no public
registry to digest-pin against, so it's a **project buildable image** (pinned by build context);
`ordo preflight` reports a missing one as "build first".

## Build
```
docker build -t ordo/qdrant-rag-mcp:latest services/qdrant-rag
```

This directory (`server.py`, `requirements.txt`, `Dockerfile`) is the single source of truth for
the service.

This image is gateway-spawned (stdio), so it appears in the rendered `mcp-registry.yaml`, not as a
long-lived compose service.
