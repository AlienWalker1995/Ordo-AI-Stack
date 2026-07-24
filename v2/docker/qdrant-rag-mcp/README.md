# qdrant-rag-mcp (Qdrant RAG MCP server)

V2's Qdrant RAG MCP image, referenced by the `qdrant-rag` plugin as `ordo/qdrant-rag-mcp:latest`.
V1 builds it locally (`ordo-ai-stack-qdrant-rag-mcp:latest`) from `C:\dev\ordo-ai-stack\qdrant-rag-mcp`
— a small Python server that embeds queries via `llamacpp-embed` and searches the Qdrant
`documents` collection. There is no public registry to digest-pin against, so it's a **project
buildable image** (pinned by build context); `ordo preflight` reports a missing one as "build first".

## Build
Build from the operator's authoritative source context (kept as the single source of truth — not
duplicated here to avoid drift), tagging the V2 image:
```
docker build -t ordo/qdrant-rag-mcp:latest C:/dev/ordo-ai-stack/qdrant-rag-mcp
```

This image is gateway-spawned (stdio), so it appears in the rendered `mcp-registry.yaml`, not as a
long-lived compose service.
