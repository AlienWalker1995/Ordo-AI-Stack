"""Locks the JSON-fragment service catalog: services/<id>/catalog.json is the single
source of truth for the dashboard's service cards.

Three invariants:
  1. Every fragment parses and every card carries the required schema keys (an explicit
     `plugin` gate, an explicit `order` so the curated grid order never depends on glob
     order, and the display fields the grid renders).
  2. The render-side aggregation (ordo.render.aggregate_services_catalog -> the
     out/services-catalog.json the dashboard mounts) is IDENTICAL to what the dashboard's
     in-repo fragment loader produces — the two implementations can't drift apart.
  3. The wiring maps derived from the fragments (OPS_SERVICE_MAP / TAILNET_LABELS) match
     the exact mappings the hardcoded catalog carried before the JSON refactor, so a
     fragment edit can't silently rewire lifecycle buttons or Open links.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard.services_catalog import (  # noqa: E402
    OPS_SERVICE_MAP,
    SERVICES,
    TAILNET_LABELS,
)
from ordo.render import aggregate_services_catalog  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAGMENTS = sorted((REPO_ROOT / "services").glob("*/catalog.json"))

REQUIRED_CARD_KEYS = {"id", "name", "order", "plugin", "category", "hint", "has_gpu"}


def test_fragments_exist():
    assert FRAGMENTS, "no services/*/catalog.json fragments found"


def test_every_fragment_parses_with_cards_list():
    for frag in FRAGMENTS:
        data = json.loads(frag.read_text(encoding="utf-8"))
        assert isinstance(data.get("cards"), list) and data["cards"], (
            f"{frag} must declare a non-empty `cards` list"
        )


def test_every_card_carries_required_schema_keys():
    for frag in FRAGMENTS:
        for card in json.loads(frag.read_text(encoding="utf-8"))["cards"]:
            missing = REQUIRED_CARD_KEYS - set(card)
            assert not missing, f"{frag} card {card.get('id')!r} missing keys: {sorted(missing)}"


def test_render_aggregation_matches_dashboard_loader():
    """out/services-catalog.json content (render side) == the dashboard's in-repo load —
    the runtime mount and the dev/test path must serve the identical card list."""
    agg = aggregate_services_catalog()
    assert agg["services"] == SERVICES


def test_orders_are_unique():
    """Explicit unique `order` per card keeps the grid layout deterministic and reviewable."""
    orders = [c["order"] for c in SERVICES]
    assert len(orders) == len(set(orders)), "duplicate `order` values across catalog fragments"


def test_derived_ops_service_map_is_exactly_the_pre_refactor_wiring():
    assert OPS_SERVICE_MAP == {
        "llamacpp": "llamacpp",
        "llamacpp-cpu": "llamacpp-cpu",
        "model-gateway": "model-gateway",
        "webui": "open-webui",
        "mcp": "mcp-gateway",
        "comfyui": "comfyui",
        "n8n": "n8n",
        "qdrant": "qdrant",
        "hermes": "hermes-dashboard",
        "stt": "stt",
        "tts": "tts",
        "rag-ingestion": "rag-ingestion",
    }


def test_derived_tailnet_labels_are_exactly_the_pre_refactor_labels():
    assert TAILNET_LABELS == {
        "webui": "chat",
        "comfyui": "comfy",
        "n8n": "n8n",
        "hermes": "hermes",
        "codebase-memory-ui": "graph",
        "model-gateway": "llm",
    }
