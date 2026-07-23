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
    present = {i for i in needed if not i.startswith("ordo/")}   # only upstream cached
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
    assert "ordo/ops-controller:latest" in imgs
    # the 5090 picks Qwen3.6, which pins the patched build — that image, not the stock one
    assert "ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470" in imgs


def test_patched_llamacpp_is_buildable_not_pullable():
    # a missing patched image must be 'build first' (blocking), never 'Docker will pull'
    rc = render(GPU, CATALOG, REGISTRY)
    needed = preflight.required_images(rc)
    patched = "ordo-ai-stack-llamacpp-patched:qwen36-swa-86b9470"
    assert patched in needed
    present = {i for i in needed if i != patched}          # everything cached except the patched build
    go, checks = preflight.run(GPU, CATALOG, REGISTRY, images_present=present)
    proj = _byname(checks)["project images built locally"]
    assert not proj.ok and proj.blocking and not go        # NO-GO — it can't be pulled
    assert "v2/docker/llamacpp-patched" in proj.detail     # points at the build context
    # it must NOT show up as a pullable upstream image
    assert not any(c.name.startswith("upstream") and patched in c.detail for c in checks)


def test_mcp_pinned_check_passes_with_real_images():
    # qdrant-rag (project image) + searxng (real digest) → the digest-pinned check is OK
    _go, checks = preflight.run(GPU, CATALOG, REGISTRY)
    mcp = _byname(checks)["all enabled MCP images digest-pinned"]
    assert mcp.ok and not mcp.blocking


def test_secrets_check_absent_file_is_skipped(tmp_path):
    # no secrets.env passed → no secrets check emitted at all (operator-managed, out-of-band)
    _go, checks = preflight.run(GPU, CATALOG, REGISTRY)
    assert not any(c.name.startswith("secrets present") for c in checks)


def test_secrets_check_warns_on_missing_keys(tmp_path):
    sec = tmp_path / "secrets.env"
    sec.write_text("LITELLM_MASTER_KEY=abc\n")             # only one of the required keys filled
    go, checks = preflight.run(GPU, CATALOG, REGISTRY, secrets_env=str(sec))
    c = next(c for c in checks if c.name.startswith("secrets present"))
    assert not c.ok and not c.blocking                     # non-blocking warning
    assert "OPS_CONTROLLER_TOKEN" in c.detail and go is True  # still GO (only a warning)


def test_secrets_check_ok_when_all_present(tmp_path):
    rc = render(GPU, CATALOG, REGISTRY)
    sec = tmp_path / "secrets.env"
    sec.write_text("".join(f"{k}=x\n" for k in rc.required_secrets))
    _go, checks = preflight.run(GPU, CATALOG, REGISTRY, secrets_env=str(sec))
    c = next(c for c in checks if c.name.startswith("secrets present"))
    assert c.ok
