"""Wizard: plan proposal, source building, and a write -> render round-trip."""
from pathlib import Path

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.hardware import HardwareProfile
from ordo.plugins import PluginRegistry
from ordo.render import render
from ordo import wizard

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")
HW_5090 = HardwareProfile.from_spec({"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128})
HW_CPU = HardwareProfile.from_spec({"gpus": [], "ram_gb": 16})


def test_plan_reflects_hardware():
    p = wizard.plan(CATALOG, REGISTRY, HW_5090)
    assert p.tier == "ultra"
    assert p.model_id == "huihui-qwen3.6-27b-q6"
    assert "song-gen" in p.plugins_available

    p_cpu = wizard.plan(CATALOG, REGISTRY, HW_CPU)
    assert "comfyui" not in p_cpu.plugins_available   # no GPU media on CPU
    assert "song-gen" not in p_cpu.plugins_available
    # light MCP tool servers still run on CPU (they're not GPU-bound)


def test_build_source_defaults_are_valid():
    src = wizard.build_source()
    assert src["agent"] == "hermes"                 # Hermes default
    assert src["model"] == "auto"
    Source.from_dict(src)                            # must be a valid declarative source


def test_build_source_honors_answers():
    src = wizard.build_source({"tier": "medium", "model": "qwen2.5-7b-instruct-q4",
                               "plugins": ["comfyui"], "agent": "hermes"})
    assert src["tier"] == "medium" and src["plugins"] == ["comfyui"]
    Source.from_dict(src)


def test_write_then_render_roundtrip(tmp_path):
    # headless wizard writes a source; render must accept it end-to-end
    out = wizard.run(CATALOG, REGISTRY, tmp_path / "ordo.yaml", interactive=False, answers={})
    assert out.exists()
    src = Source.load(out)
    rc = render(src, CATALOG, REGISTRY)
    assert rc.model.id and rc.ctx_size > 0
