"""The CPU model keeps each caller's context in its RAM prompt cache across turns.

`inference-engine-cpu` serves one request at a time on one slot, so callers take turns on it. On
llama.cpp's default slot selection the outgoing context is saved to the prompt cache only when the
new request keeps under half of it; callers sharing a long system prompt keep more than that, so
nothing was saved and every switch re-read ~25k tokens (7-15 minutes a turn, 2026-10-30). Slot
selection by LRU (`--slot-prompt-similarity 0`) saves and restores on every request, and the cache
size is pinned rather than left to the upstream default.
"""
from __future__ import annotations

from pathlib import Path

from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")


def _command() -> list[str]:
    hardware = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "compute_cap": "12.0", "uuid": "GPU-aaaa"}],
                "ram_gb": 128, "cpu_cores": 48, "platform": "Linux"}
    rc = render(Source.from_dict({"hardware": hardware, "plugins": ["inference-engine-cpu"]}), CATALOG, REGISTRY)
    return [str(arg) for arg in rc.compose_dict()["services"]["inference-engine-cpu"]["command"]]


def _flag(command: list[str], flag: str) -> str:
    assert command.count(flag) == 1, f"{flag} must appear exactly once in {command}"
    return command[command.index(flag) + 1]


def test_the_slot_is_chosen_by_lru_so_every_switch_saves_the_outgoing_context():
    assert _flag(_command(), "--slot-prompt-similarity") == "0"


def test_the_prompt_cache_size_is_pinned():
    assert _flag(_command(), "--cache-ram") == "8192"


def test_the_slot_still_serves_one_request_at_a_time():
    assert _flag(_command(), "--parallel") == "1"


def test_the_ram_gate_counts_the_prompt_cache():
    plugin = next(p for p in REGISTRY.plugins if p.id == "inference-engine-cpu")
    assert plugin.ram_gb >= 33
