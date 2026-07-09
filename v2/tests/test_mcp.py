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
    # mcp servers are NOT compose services — plugins_enabled holds only kind=service plugins
    assert "qdrant-rag" not in rc.plugins_enabled and "searxng" not in rc.plugins_enabled


def test_mcp_tools_available_even_on_cpu():
    rc = render(_src(hardware=P_CPU), CATALOG, REGISTRY)
    # no GPU → no GPU media plugins (monitoring is CPU-ok and stays)
    assert not ({"comfyui", "song-gen", "voice"} & set(rc.plugins_enabled))
    assert {s["id"] for s in rc.mcp_servers} >= {"qdrant-rag", "searxng"}  # but tools still work


def test_real_mcp_images_do_not_warn():
    # qdrant-rag = project buildable image (pinned by build context); searxng = real registry digest.
    # Neither should trip the unpinned/placeholder warnings anymore.
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    assert not any("placeholder" in w for w in rc.warnings)
    assert not any("not digest-pinned" in w for w in rc.warnings)


def test_placeholder_digest_still_detected():
    # the placeholder/unpinned detection itself must still fire for a bad public digest
    from ordo.plugins import Plugin
    from ordo.render import _render_mcp
    bad = Plugin.from_dict({"id": "bad", "kind": "mcp",
                            "mcp": {"image": "mcp/x@sha256:" + "0" * 64}})
    _servers, notes = _render_mcp([bad])
    assert any("placeholder" in n for n in notes)


def test_write_emits_mcp_registry_yaml(tmp_path):
    render(_src(hardware=P_5090), CATALOG, REGISTRY).write(tmp_path)
    reg = yaml.safe_load((tmp_path / "mcp-registry.yaml").read_text())
    assert {s["id"] for s in reg["servers"]} >= {"qdrant-rag", "searxng"}
