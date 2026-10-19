# Component: RAG Pipeline

## Purpose

Retrieval-Augmented Generation (RAG) pipeline providing vector search and document ingestion. Qdrant stores embeddings; `rag-ingestion` watches a directory and chunks/embeds/stores documents automatically.

## Services

- **Qdrant** (`:6333`, backend-only) — Vector database
- **rag-ingestion** (`--profile rag`) — Watch-mode document ingester

## Ingest Flow

1. Drop documents into `data/rag-input/` (the ingester also watches the memory vault and this repo's `docs/`, read-only)
2. `rag-ingestion` watches directory; chunks at `RAG_CHUNK_SIZE` tokens (default 400, overlap 50)
3. Embeds directly against the `inference-engine-embed` service (`http://inference-engine-embed:8080`) — **not**
   the LiteLLM model gateway; the env var carrying that URL is confusingly still named
   `MODEL_GATEWAY_URL` (`EMBED_URL` is the current/preferred name, matching its sibling
   `services/qdrant-rag/server.py`). Model is `EMBED_MODEL`, default `embeddinggemma-2-Q8_0.gguf`.
4. Stores in Qdrant collection (`RAG_COLLECTION`, default `documents`)

## Query Flow

Open WebUI → Qdrant (`VECTOR_DB=qdrant`, `QDRANT_URI=http://qdrant:6333`) — configured automatically in compose.

## Status API

The dashboard's `GET /api/overview` reports the collection point count as `knowledge.documents` (`null` when Qdrant is unreachable).

## User Flow

```
1. ordo up qdrant rag-ingestion                 # from the repo root (see docs/operator-guide.md)
2. cp document.pdf data/rag-input/              # drop document
3. rag-ingestion chunks + embeds + stores       # automatic
4. Open WebUI chat → toggle RAG                 # retrieves relevant chunks
```

## Configuration

```yaml
# out/docker-compose.yml (rendered from ordo.yaml; relevant env vars)
rag-ingestion:
  environment:
    - MODEL_GATEWAY_URL=http://inference-engine-embed:8080   # despite the name, points at inference-engine-embed
    - EMBED_MODEL=${EMBED_MODEL:-${LLAMACPP_EMBED_MODEL:-embeddinggemma-2-Q8_0.gguf}}
    - QDRANT_COLLECTION=${RAG_COLLECTION:-documents}
    - CHUNK_SIZE=${RAG_CHUNK_SIZE:-400}
    - CHUNK_OVERLAP=${RAG_CHUNK_OVERLAP:-50}
```

## Dependencies

- **Qdrant** service (on the single `ordo-net`; internal, no host port)
- **`inference-engine-embed`** (services/rag/plugin.yaml) for embeddings — EmbeddingGemma 2 (GGUF) served
  directly by llama.cpp, not routed through the LiteLLM model gateway
- `embeddinggemma-2-Q8_0.gguf` (or `LLAMACPP_EMBED_MODEL` override) must be present before
  ingestion can embed

## Non-Goals

- Replacing Open WebUI's built-in vector store — RAG is an enhancement
- Managing document lifecycle (retention, versioning)
