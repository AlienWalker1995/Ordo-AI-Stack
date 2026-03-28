# Component: RAG Pipeline

## Purpose

Retrieval-Augmented Generation (RAG) pipeline providing vector search and document ingestion. Qdrant stores embeddings; `rag-ingestion` watches a directory and chunks/embeds/stores documents automatically.

## Services

- **Qdrant** (`:6333`, backend-only) — Vector database
- **rag-ingestion** (`--profile rag`) — Watch-mode document ingester

## Ingest Flow

1. Drop documents into `data/rag-input/`
2. `rag-ingestion` watches directory; chunks at `RAG_CHUNK_SIZE` tokens (default 400, overlap 50)
3. Embeds via model gateway (`EMBED_MODEL`, default `nomic-embed-text`)
4. Stores in Qdrant collection (`RAG_COLLECTION`, default `documents`)

## Query Flow

Open WebUI → Qdrant (`VECTOR_DB=qdrant`, `QDRANT_URI=http://qdrant:6333`) — configured automatically in compose.

## Status API

`GET /api/rag/status` → `{ok, collection, points_count, status}` — auth-exempt so dashboard can always display it.

## User Flow

```
1. ./compose --profile rag up -d          # start Qdrant + rag-ingestion
2. cp document.pdf data/rag-input/        # drop document
3. rag-ingestion chunks + embeds + stores # automatic
4. Open WebUI chat → toggle RAG           # retrieves relevant chunks
```

## Configuration

```yaml
# docker-compose.yml (relevant env vars)
rag-ingestion:
  environment:
    - EMBED_MODEL=${EMBED_MODEL:-nomic-embed-text}
    - QDRANT_COLLECTION=${RAG_COLLECTION:-documents}
    - CHUNK_SIZE=${RAG_CHUNK_SIZE:-400}
    - CHUNK_OVERLAP=${RAG_CHUNK_OVERLAP:-50}
```

## Dependencies

- **Qdrant** service on backend network
- **Model gateway** for embeddings
- `nomic-embed-text` model must be pulled before ingestion can embed

## Non-Goals

- Replacing Open WebUI's built-in vector store — RAG is an enhancement
- Managing document lifecycle (retention, versioning)
