# Component: RAG Pipeline

## Purpose

Retrieval-Augmented Generation (RAG) pipeline providing vector search and document ingestion. Qdrant stores embeddings; `rag-ingestion` watches a directory and chunks/embeds/stores documents automatically.

## Services

- **Qdrant** (`:6333`, backend-only) — Vector database
- **rag-ingestion** (`--profile rag`) — Watch-mode document ingester

## Ingest Flow

1. Drop documents into `data/rag-input/`
2. `rag-ingestion` watches directory; chunks at `RAG_CHUNK_SIZE` tokens (default 400, overlap 50)
3. Embeds directly against the `llamacpp-embed` service (`http://llamacpp-embed:8080`) — **not**
   the LiteLLM model gateway; the env var carrying that URL is confusingly still named
   `MODEL_GATEWAY_URL` (`EMBED_URL` is the current/preferred name, matching its sibling
   `services/qdrant-rag/server.py`). Model is `EMBED_MODEL`, default `nomic-embed-text-v1.5.Q4_K_M.gguf`.
4. Stores in Qdrant collection (`RAG_COLLECTION`, default `documents`)

## Query Flow

Open WebUI → Qdrant (`VECTOR_DB=qdrant`, `QDRANT_URI=http://qdrant:6333`) — configured automatically in compose.

## Status API

`GET /api/rag/status` → `{ok, collection, points_count, status}` — auth-exempt so dashboard can always display it.

## User Flow

```
1. docker compose -p ordo --profile rag up -d   # from out/ (see docs/operator-guide.md); start Qdrant + rag-ingestion
2. cp document.pdf data/rag-input/              # drop document
3. rag-ingestion chunks + embeds + stores       # automatic
4. Open WebUI chat → toggle RAG                 # retrieves relevant chunks
```

## Configuration

```yaml
# out/docker-compose.yml (rendered from ordo.yaml; relevant env vars)
rag-ingestion:
  environment:
    - MODEL_GATEWAY_URL=http://llamacpp-embed:8080   # despite the name, points at llamacpp-embed
    - EMBED_MODEL=${EMBED_MODEL:-${LLAMACPP_EMBED_MODEL:-nomic-embed-text-v1.5.Q4_K_M.gguf}}
    - QDRANT_COLLECTION=${RAG_COLLECTION:-documents}
    - CHUNK_SIZE=${RAG_CHUNK_SIZE:-400}
    - CHUNK_OVERLAP=${RAG_CHUNK_OVERLAP:-50}
```

## Dependencies

- **Qdrant** service on backend network
- **`llamacpp-embed`** (services/rag/plugin.yaml) for embeddings — a small nomic GGUF served
  directly by llama.cpp, not routed through the LiteLLM model gateway
- `nomic-embed-text-v1.5.Q4_K_M.gguf` (or `LLAMACPP_EMBED_MODEL` override) must be present before
  ingestion can embed

## Non-Goals

- Replacing Open WebUI's built-in vector store — RAG is an enhancement
- Managing document lifecycle (retention, versioning)
