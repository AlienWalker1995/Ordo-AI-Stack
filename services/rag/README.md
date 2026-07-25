# rag-ingestion (folder-watch ingester)

Build context for the RAG ingester image, referenced by the `rag` plugin
([`plugin.yaml`](plugin.yaml)) as `ordo/rag-ingestion:latest`. A small Python service that watches
a folder, chunks + embeds documents against `llamacpp-embed`, and upserts to Qdrant (`documents`
collection, 768-dim nomic space, matching the qdrant-rag MCP's query vectors). Project buildable
image, so `ordo preflight` reports a missing one as "build first".

## Build
```
docker build -t ordo/rag-ingestion:latest services/rag
```

This directory is the single source of truth for the service (`ingest.py`, `requirements.txt`,
`Dockerfile`) — the manifest references the image by name, so it can't drift from the build context.
