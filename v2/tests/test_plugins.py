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
# The real host: primary 5090 (compute) + secondary Pascal 1070 (voice must land here).
UUID_5090 = "GPU-97fe65ee-5e2d-8c9b-32d0-362f510ceb96"
UUID_1070 = "GPU-20fac13a-5e5b-1818-581f-63901612fd84"
P_DUAL = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": UUID_5090},
                   {"name": "GTX 1070", "vram_gb": 8, "uuid": UUID_1070}],
          "ram_gb": 128, "cpu_cores": 32}


def _src(**kw):
    base = {"hardware": "auto", "tier": "auto", "model": "auto", "plugins": "auto"}
    base.update(kw)
    return Source.from_dict(base)


# The CPU-ok service plugins ported for V1 parity — enable on ANY hardware (they run without a GPU),
# but stay dormant behind their compose profile until requested. Voice/comfyui/song-gen are the
# GPU-gated ones handled separately.
CPU_OK_SERVICE_PLUGINS = {"monitoring", "rag", "worker", "automation", "open-webui",
                          "searxng-web", "codebase-memory-ui", "hermes-dashboard", "edge"}


def test_registry_loaded_manifests():
    ids = {p.id for p in REGISTRY.plugins}
    assert {"comfyui", "song-gen", "voice", "monitoring"} <= ids
    # the ported V1-parity plugins are registered too
    assert {"rag", "worker", "automation", "open-webui", "searxng-web",
            "codebase-memory-ui", "hermes-dashboard", "edge"} <= ids


def test_big_gpu_enables_all_and_merges_env():
    # single 5090: media enables; the CPU-ok service plugins enable; voice needs a SECOND card → off
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    # worker depends on comfyui (enabled here), so it's in the set too
    assert set(rc.plugins_enabled) == {"comfyui", "song-gen"} | CPU_OK_SERVICE_PLUGINS
    assert "voice" not in rc.plugins_enabled
    assert rc.env["COMFYUI_ENABLED"] == "1"
    assert rc.env["SONG_GEN_ENABLED"] == "1"
    assert rc.env["RAG_ENABLED"] == "1"           # a ported plugin's env fragment merges too
    assert "media" in rc.compose_profiles and "rag" in rc.compose_profiles


def test_dual_gpu_enables_voice_pinned_to_secondary():
    # 5090 + 1070: everything including voice; voice pins to the 1070's uuid (Pascal kernels)
    rc = render(_src(hardware=P_DUAL), CATALOG, REGISTRY)
    assert {"comfyui", "song-gen", "voice", "monitoring"} <= set(rc.plugins_enabled)
    assert "voice" in rc.compose_profiles
    c = rc.compose_dict()
    for svc in ("stt", "tts"):
        env = c["services"][svc]["environment"]
        assert env["CUDA_VISIBLE_DEVICES"] == UUID_1070
        assert env["NVIDIA_VISIBLE_DEVICES"] == UUID_1070
        dev = c["services"][svc]["deploy"]["resources"]["reservations"]["devices"][0]
        assert dev["device_ids"] == [UUID_1070]           # pinned card, not `count: all`


def test_single_gpu_disables_voice_with_warning():
    # only the 5090: voice images crash there → gated OFF, never fall back to the primary
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    assert "voice" not in rc.plugins_enabled
    assert any("SECONDARY GPU" in w for w in rc.warnings)


def test_cpu_disables_voice_and_media():
    rc = render(_src(hardware=P_CPU), CATALOG, REGISTRY)
    assert "voice" not in rc.plugins_enabled              # CPU-only → voice off
    assert not ({"comfyui", "song-gen"} & set(rc.plugins_enabled))
    assert "COMFYUI_ENABLED" not in rc.env
    # worker depends on comfyui (GPU-only) → dropped on CPU; the rest of the CPU-ok set stays
    assert set(rc.plugins_enabled) == CPU_OK_SERVICE_PLUGINS - {"worker"}
    assert "worker" not in rc.plugins_enabled


def test_small_gpu_gates_by_vram():
    # single 8GB card: comfyui(6) fits; song-gen(20) does not; voice needs a 2nd card → off
    rc = render(_src(hardware=P_8GB), CATALOG, REGISTRY)
    assert "comfyui" in rc.plugins_enabled
    assert "voice" not in rc.plugins_enabled
    assert "song-gen" not in rc.plugins_enabled


def test_dependency_drops_plugin_when_dep_absent():
    # explicitly ask for song-gen only (no comfyui) → dep unmet → dropped with a note
    rc = render(_src(hardware=P_5090, plugins=["song-gen"]), CATALOG, REGISTRY)
    assert "song-gen" not in rc.plugins_enabled
    assert any("dependency" in w for w in rc.warnings)


def test_explicit_list_respected():
    rc = render(_src(hardware=P_5090, plugins=["comfyui"]), CATALOG, REGISTRY)
    assert rc.plugins_enabled == ["comfyui"]
