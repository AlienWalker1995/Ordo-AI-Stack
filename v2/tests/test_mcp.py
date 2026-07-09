"""kind=mcp plugins: rendered into a pinned mcp-gateway registry, drift-free."""
from pathlib import Path

import yaml

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")
P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128}
P_CPU = {"gpus": [], "ram_gb": 16}


def _src(**kw):
    base = {"hardware": "auto", "tier": "auto", "model": "auto", "plugins": "auto"}
    base.update(kw)
    return Source.from_dict(base)


def test_mcp_plugins_loaded():
    mcp = {p.id for p in REGISTRY.plugins if p.kind == "mcp"}
    assert {"qdrant-rag", "searxng"} <= mcp


def test_render_emits_mcp_registry():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    ids = {s["id"] for s in rc.mcp_servers}
    assert {"qdrant-rag", "searxng"} <= ids
    q = next(s for s in rc.mcp_servers if s["id"] == "qdrant-rag")
    assert "qdrant_search" in q["tools"] and q["env"]["QDRANT_URL"]
    # mcp servers are NOT compose services — plugins_enabled stays media-only
    assert set(rc.plugins_enabled) == {"comfyui", "song-gen", "voice"}


def test_mcp_tools_available_even_on_cpu():
    rc = render(_src(hardware=P_CPU), CATALOG, REGISTRY)
    assert rc.plugins_enabled == []                       # no GPU media
    assert {s["id"] for s in rc.mcp_servers} >= {"qdrant-rag", "searxng"}  # but tools still work


def test_placeholder_image_warns():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    assert any("placeholder" in w for w in rc.warnings)   # our sample shas are placeholders


def test_write_emits_mcp_registry_yaml(tmp_path):
    render(_src(hardware=P_5090), CATALOG, REGISTRY).write(tmp_path)
    reg = yaml.safe_load((tmp_path / "mcp-registry.yaml").read_text())
    assert {s["id"] for s in reg["servers"]} >= {"qdrant-rag", "searxng"}
