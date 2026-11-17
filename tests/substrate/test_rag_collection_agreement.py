"""The collection the indexer writes is the collection the search MCP server reads.

`RAG_COLLECTION` is one operator setting (a `site:` key). rag-ingestion upserts into it and
mcp-qdrant-rag searches it, so the rendered stack must hand both the same value.
"""
from __future__ import annotations

from pathlib import Path

from ordo.render import stack
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
HARDWARE = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128}


def _resolved_collections(site: dict[str, str]) -> tuple[str, str]:
    """(indexer collection, search server collection) as compose would resolve them."""
    source = Source.from_dict(
        {"hardware": HARDWARE, "tier": "auto", "model": "auto", "plugins": ["rag", "qdrant-rag"], "site": site}
    )
    rendered = render(source, CATALOG, REGISTRY)
    services = rendered.compose_dict()["services"]
    indexer = services["rag-ingestion"]["environment"]["QDRANT_COLLECTION"]
    searcher = services["mcp-qdrant-rag"]["environment"]["RAG_COLLECTION"]
    return stack.expand_env(indexer, rendered.env), stack.expand_env(searcher, rendered.env)


def test_indexer_and_search_server_share_an_operator_collection():
    indexer, searcher = _resolved_collections({"RAG_COLLECTION": "team_notes"})
    assert indexer == "team_notes"
    assert searcher == indexer


def test_indexer_and_search_server_share_the_default_collection():
    indexer, searcher = _resolved_collections({})
    assert indexer == "documents"
    assert searcher == indexer
