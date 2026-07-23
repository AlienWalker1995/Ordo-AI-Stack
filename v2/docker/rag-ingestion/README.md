# rag-ingestion (folder-watch ingester)

V2's RAG ingester, referenced by the `rag` plugin as `ordo/rag-ingestion:latest`. V1 builds it
locally from `C:\dev\ordo-ai-stack\rag-ingestion` — a small Python service that watches a folder,
chunks + embeds documents against `llamacpp-embed`, and upserts to Qdrant (`documents` collection,
768-dim nomic space, matching the qdrant-rag MCP's query vectors). Project buildable image, so
`ordo preflight` reports a missing one as "build first".

## Build
```
docker build -t ordo/rag-ingestion:latest C:/dev/ordo-ai-stack/rag-ingestion
```

The V1 build context is the single source of truth for this small service; it is referenced (not
duplicated) so the two can't drift.
