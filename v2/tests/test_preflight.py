"""Preflight is a read-only GO/NO-GO gate: drift, checksums, GPU, parity, images."""
from pathlib import Path

from ordo import preflight
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")

GPU = Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                        "model": "auto", "plugins": "auto"})
CPU = Source.from_dict({"hardware": {"gpus": [], "ram_gb": 32}, "model": "auto", "plugins": "auto"})


def _byname(checks):
    return {c.name: c for c in checks}


def test_config_and_ctx_consistency_is_blocking_and_passes():
    go, checks = preflight.run(GPU, CATALOG, REGISTRY)
    c = next(c for c in checks if c.name.startswith("config renders"))
    assert c.blocking and c.ok


def test_go_when_all_blocking_pass_without_image_check():
    go, checks = preflight.run(GPU, CATALOG, REGISTRY)
    assert go is True                                   # no blocking check fails on a clean 5090 source


def test_gpu_plugin_without_gpu_is_no_go():
    # force the media plugin onto CPU hardware -> blocking GPU check fails
    src = Source.from_dict({"hardware": {"gpus": [], "ram_gb": 64},
                            "model": "auto", "plugins": ["comfyui"]})
    go, checks = preflight.run(src, CATALOG, REGISTRY)
    gpu_check = next(c for c in checks if c.name.startswith("GPU present"))
    # comfyui is gated out on CPU by the registry, so it won't be enabled -> check passes & GO.
    # (the registry prevents the impossible config from ever rendering — that's the point)
    assert gpu_check.ok and go


def test_missing_project_image_blocks():
    rc = render(GPU, CATALOG, REGISTRY)
    needed = preflight.required_images(rc)
    present = {i for i in needed if not i.startswith("ordo-v2/")}   # only upstream cached
    go, checks = preflight.run(GPU, CATALOG, REGISTRY, images_present=present)
    img = _byname(checks)["project images built locally"]
    assert not img.ok and img.blocking and not go                  # NO-GO until images are built


def test_all_images_present_is_go():
    rc = render(GPU, CATALOG, REGISTRY)
    present = set(preflight.required_images(rc))
    go, checks = preflight.run(GPU, CATALOG, REGISTRY, images_present=present)
    assert _byname(checks)["project images built locally"].ok and go


def test_parity_mismatch_is_no_go(tmp_path):
    ref = tmp_path / ".env"
    ref.write_text("LLAMACPP_CTX_SIZE=999999\n")                   # deliberately wrong
    go, checks = preflight.run(GPU, CATALOG, REGISTRY, ref_env=str(ref))
    par = next(c for c in checks if c.name.startswith("parity"))
    assert not par.ok and par.blocking and not go


def test_required_images_include_core_and_ops():
    rc = render(GPU, CATALOG, REGISTRY)
    imgs = preflight.required_images(rc)
    assert "ordo-v2/ops-controller:latest" in imgs
    assert any("llama.cpp" in i for i in imgs)
