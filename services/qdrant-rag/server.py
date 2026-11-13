#!/usr/bin/env python3
"""MCP server: semantic search over the stack's Qdrant RAG collection.

Embeds the query with the SAME local embedder used by rag-ingestion
(inference-engine-embed, EmbeddingGemma 2, 768-dim) so query vectors live in the same
space as the stored chunks, then runs a Qdrant similarity search. The query
gets the model's query prompt (EMBED_QUERY_PREFIX); rag-ingestion gives each
chunk the matching document prompt. Exposed to
Hermes through the LiteLLM MCP gateway as qdrant-rag-qdrant_search.

Authority: rag-ingestion stores each chunk's `authority` (canonical / derived / ephemeral, decided
by where the file lives), `status` and `last_verified`. A default search leaves out history
(status archived or superseded) and the unreviewed inbox (ephemeral), ranks canonical docs (the
PRDs) above other matches of similar similarity, and returns those fields with every result so the
agent can tell a source of truth from a note that may be stale.

Why embed via inference-engine-embed directly (not model-gateway/litellm): litellm's
/v1/embeddings route currently 500s for the local embed model; the raw
llama.cpp embedding server works and ignores the model field.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
EMBED_URL = os.environ.get("EMBED_URL", "http://inference-engine-embed:8080").rstrip("/")
COLLECTION = os.environ.get("RAG_COLLECTION", "documents").strip()
EMBED_QUERY_PREFIX = os.environ.get("EMBED_QUERY_PREFIX", "")

# Status values that mark a document as history, left out unless the caller asks for them.
HISTORY_STATUSES = ["archived", "superseded"]
# Added to a canonical chunk's similarity before ranking: enough to put the PRD first among close
# matches, not enough to lift an unrelated one over a clearly better note.
CANONICAL_BOOST = 0.05
# Candidates fetched per result returned, so the boost can reorder beyond the plain top `limit`.
CANDIDATES_PER_RESULT = 3

mcp = FastMCP("qdrant-rag", host="0.0.0.0", port=9000, stateless_http=True)


def search_filter(include_history: bool) -> dict | None:
    """The Qdrant filter of a search. Chunks indexed before metadata existed carry no `status` or
    `authority` and are kept: a must_not on a missing field excludes nothing."""
    if include_history:
        return None
    return {"must_not": [
        {"key": "status", "match": {"any": HISTORY_STATUSES}},
        {"key": "authority", "match": {"value": "ephemeral"}},
    ]}


def rank(hits: list[dict], limit: int) -> list[dict]:
    """The `limit` best hits after the canonical boost, as tool results with their provenance."""
    def ranked_score(hit: dict) -> float:
        payload = hit.get("payload") or {}
        return hit.get("score", 0.0) + (CANONICAL_BOOST if payload.get("authority") == "canonical" else 0.0)

    results = []
    for hit in sorted(hits, key=ranked_score, reverse=True)[:limit]:
        payload = hit.get("payload") or {}
        results.append({
            "score": round(hit.get("score", 0.0), 4),
            "source": payload.get("source"),
            "authority": payload.get("authority", "unknown"),
            "status": payload.get("status", "unknown"),
            "last_verified": payload.get("last_verified"),
            "chunk_index": payload.get("chunk_index"),
            "content": payload.get("content", ""),
        })
    return results


def _embed(text: str) -> list[float]:
    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            f"{EMBED_URL}/v1/embeddings",
            json={"input": [text]},
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
    return data["data"][0]["embedding"]


@mcp.tool()
def qdrant_search(query: str, limit: int = 5, include_history: bool = False) -> dict[str, Any]:
    """Semantic search over indexed documents in Qdrant.

    Use this to retrieve relevant indexed knowledge (docs, code, notes) instead
    of pasting large context into the prompt. Returns the top matching chunks
    with their source file, similarity score and provenance:
      - authority "canonical": the operator's source-of-truth docs (the PRDs). When a canonical
        result and another result disagree, the canonical one wins.
      - authority "derived": notes and other docs. They may be out of date: check last_verified,
        and verify against the code or live state before acting on them.
    Archived and superseded documents and the unreviewed inbox are left out unless
    include_history is true (use it only to research what used to be true).

    Args:
        query: Natural-language search query.
        limit: Max number of chunks to return (default 5).
        include_history: Also return archived, superseded and inbox documents (default false).
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is empty"}
    limit = max(1, min(int(limit or 5), 25))
    try:
        vector = _embed(EMBED_QUERY_PREFIX + q)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"embedding failed: {exc}"}
    try:
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
                json={"vector": vector, "limit": limit * CANDIDATES_PER_RESULT, "with_payload": True,
                      "filter": search_filter(include_history)},
            )
            if r.status_code == 404:
                return {"error": f"collection '{COLLECTION}' does not exist yet — nothing has been ingested",
                        "results": []}
            r.raise_for_status()
            hits = r.json().get("result", [])
    except Exception as exc:  # noqa: BLE001
        return {"error": f"qdrant search failed: {exc}"}

    results = rank(hits, limit)
    return {"collection": COLLECTION, "count": len(results), "results": results}


@mcp.tool()
def qdrant_status() -> dict[str, Any]:
    """List Qdrant collections and the indexed point count of the RAG collection."""
    try:
        with httpx.Client(timeout=30.0) as client:
            cols = client.get(f"{QDRANT_URL}/collections")
            cols.raise_for_status()
            names = [c["name"] for c in cols.json().get("result", {}).get("collections", [])]
            count = None
            if COLLECTION in names:
                c = client.get(f"{QDRANT_URL}/collections/{COLLECTION}")
                c.raise_for_status()
                count = c.json().get("result", {}).get("points_count")
        return {"collections": names, "rag_collection": COLLECTION, "points_count": count}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"qdrant status failed: {exc}"}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
