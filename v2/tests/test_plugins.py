"""Registry-driven plugin resolution: hardware gating, dependencies, env composition."""
from pathlib import Path

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")

P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128, "cpu_cores": 32}
P_CPU = {"gpus": [], "ram_gb": 16, "cpu_cores": 8}
P_8GB = {"gpus": [{"name": "RTX 3070", "vram_gb": 8}], "ram_gb": 32, "cpu_cores": 12}


def _src(**kw):
    base = {"hardware": "auto", "tier": "auto", "model": "auto", "plugins": "auto"}
    base.update(kw)
    return Source.from_dict(base)


def test_registry_loaded_manifests():
    ids = {p.id for p in REGISTRY.plugins}
    assert {"comfyui", "song-gen", "voice"} <= ids


def test_big_gpu_enables_all_and_merges_env():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    assert set(rc.plugins_enabled) == {"comfyui", "song-gen", "voice"}
    assert rc.env["COMFYUI_ENABLED"] == "1"
    assert rc.env["SONG_GEN_ENABLED"] == "1"
    assert "media" in rc.compose_profiles


def test_cpu_disables_all_media():
    rc = render(_src(hardware=P_CPU), CATALOG, REGISTRY)
    assert rc.plugins_enabled == []
    assert "COMFYUI_ENABLED" not in rc.env


def test_small_gpu_gates_by_vram():
    # 8GB card: comfyui(6) + voice(4) fit; song-gen(20) does not
    rc = render(_src(hardware=P_8GB), CATALOG, REGISTRY)
    assert "comfyui" in rc.plugins_enabled
    assert "voice" in rc.plugins_enabled
    assert "song-gen" not in rc.plugins_enabled


def test_dependency_drops_plugin_when_dep_absent():
    # explicitly ask for song-gen only (no comfyui) → dep unmet → dropped with a note
    rc = render(_src(hardware=P_5090, plugins=["song-gen"]), CATALOG, REGISTRY)
    assert "song-gen" not in rc.plugins_enabled
    assert any("dependency" in w for w in rc.warnings)


def test_explicit_list_respected():
    rc = render(_src(hardware=P_5090, plugins=["comfyui"]), CATALOG, REGISTRY)
    assert rc.plugins_enabled == ["comfyui"]
