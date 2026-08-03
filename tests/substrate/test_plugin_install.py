"""ControlPlane /plugins — Hermes-driven service install.

The render authority: enable/disable a service plugin the drift-safe way (edit ordo.yaml's plugins
list -> re-render -> regenerate out/), the SAME one-write-path as the model switch. Under
`plugins: auto` a fitting plugin is already rendered (dormant behind its profile), so enable is a
no-op edit; on an explicit list it edits + re-renders. Core/edge/agent are refused (allowlist);
unfittable plugins are refused with the resolve note.
"""
from pathlib import Path

import yaml

from ordo.catalog import Catalog
from ordo.control import INSTALLABLE_PLUGINS, ControlPlane
from ordo.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")


def _cp(tmp_path, plugins="auto", gpus=None, ram_gb=128):
    src = tmp_path / "ordo.yaml"
    hw = {"gpus": gpus if gpus is not None else [{"vram_gb": 32}], "ram_gb": ram_gb}
    src.write_text(yaml.safe_dump({"hardware": hw, "model": "auto", "plugins": plugins},
                                  sort_keys=False))
    return ControlPlane(src, CATALOG, REGISTRY, tmp_path / "out"), src


def test_list_plugins_returns_installable_catalog(tmp_path):
    cp, _ = _cp(tmp_path)
    code, body = cp.route("GET", "/plugins")
    assert code == 200
    assert {p["id"] for p in body["plugins"]} == set(INSTALLABLE_PLUGINS)   # exactly the allowlist
    ow = next(p for p in body["plugins"] if p["id"] == "open-webui")
    assert "open-webui" in ow["services"] and ow["compose_profile"] == "webui"
    assert isinstance(ow["fits"], bool) and isinstance(ow["enabled"], bool)


def test_enable_under_auto_is_already_rendered_no_write(tmp_path):
    cp, src = _cp(tmp_path, plugins="auto")
    before = src.read_text()
    code, body = cp.route("POST", "/plugins/comfyui/enable", {"confirm": True})
    assert code == 200 and body["ok"] and body["already_rendered"] is True
    assert "comfyui" in body["services"]
    assert src.read_text() == before          # auto: pre-rendered dormant -> nothing written


def test_enable_on_explicit_list_edits_source_and_rerenders(tmp_path):
    cp, src = _cp(tmp_path, plugins=["comfyui"])
    code, body = cp.route("POST", "/plugins/open-webui/enable", {"confirm": True})
    assert code == 200 and body["ok"] and body["already_rendered"] is False
    plugins = yaml.safe_load(src.read_text())["plugins"]
    assert "open-webui" in plugins and "rag" in plugins    # dependency auto-added
    assert "open-webui" in (tmp_path / "out" / "docker-compose.yml").read_text()


def test_enable_non_installable_is_403_and_writes_nothing(tmp_path):
    cp, src = _cp(tmp_path, plugins=["comfyui"])
    before = src.read_text()
    for pid in ("agent", "edge", "llamacpp"):
        code, body = cp.route("POST", f"/plugins/{pid}/enable", {"confirm": True})
        assert code == 403 and "error" in body
    assert src.read_text() == before


def test_enable_unfittable_is_409_and_writes_nothing(tmp_path):
    cp, src = _cp(tmp_path, plugins=["comfyui"], gpus=[{"vram_gb": 32}])   # ONE gpu
    before = src.read_text()
    code, body = cp.route("POST", "/plugins/voice/enable", {"confirm": True})  # voice needs a 2nd GPU
    assert code == 409 and "error" in body
    assert src.read_text() == before


def test_disable_on_explicit_list_removes_and_rerenders(tmp_path):
    cp, src = _cp(tmp_path, plugins=["comfyui", "rag"])
    code, body = cp.route("POST", "/plugins/rag/disable", {"confirm": True})
    assert code == 200 and body["ok"]
    assert "rag" not in yaml.safe_load(src.read_text())["plugins"]


def test_disable_under_auto_is_transient(tmp_path):
    cp, _ = _cp(tmp_path, plugins="auto")
    code, body = cp.route("POST", "/plugins/comfyui/disable", {"confirm": True})
    assert code == 200 and body.get("transient") is True
