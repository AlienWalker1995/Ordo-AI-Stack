#!/usr/bin/env python3
"""MCP server: semantic search over the stack's Qdrant RAG collection.

Embeds the query with the SAME local nomic embedder used by rag-ingestion
(llamacpp-embed, 768-dim, no task prefix) so query vectors live in the same
space as the stored chunks, then runs a Qdrant similarity search. Exposed to
Hermes via the mcp-gateway as gateway/qdrant_search.

Why embed via llamacpp-embed directly (not model-gateway/litellm): litellm's
/v1/embeddings route currently 500s for the local embed model; the raw
llama.cpp embedding server works and ignores the model field.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333").rstrip("/")
EMBED_URL = os.environ.get("EMBED_URL", "http://llamacpp-embed:8080").rstrip("/")
COLLECTION = os.environ.get("RAG_COLLECTION", "documents").strip()

mcp = FastMCP("qdrant-rag")


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
def qdrant_search(query: str, limit: int = 5) -> dict[str, Any]:
    """Semantic search over indexed documents in Qdrant.

    Use this to retrieve relevant indexed knowledge (docs, code, notes) instead
    of pasting large context into the prompt. Returns the top matching chunks
    with their source file and similarity score.

    Args:
        query: Natural-language search query.
        limit: Max number of chunks to return (default 5).
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is empty"}
    limit = max(1, min(int(limit or 5), 25))
    try:
        vector = _embed(q)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"embedding failed: {exc}"}
    try:
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
                json={"vector": vector, "limit": limit, "with_payload": True},
            )
            if r.status_code == 404:
                return {"error": f"collection '{COLLECTION}' does not exist yet — nothing has been ingested",
                        "results": []}
            r.raise_for_status()
            hits = r.json().get("result", [])
    except Exception as exc:  # noqa: BLE001
        return {"error": f"qdrant search failed: {exc}"}

    results = []
    for h in hits:
        p = h.get("payload", {}) or {}
        results.append({
            "score": round(h.get("score", 0.0), 4),
            "source": p.get("source"),
            "chunk_index": p.get("chunk_index"),
            "content": p.get("content", ""),
        })
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
    mcp.run()
