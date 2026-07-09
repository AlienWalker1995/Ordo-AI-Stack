"""Acceptance gate for the config render engine (first slice)."""
import json
from pathlib import Path

import pytest

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")

PROFILE_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128,
                "cpu_cores": 32, "platform": "Linux"}
PROFILE_CPU = {"gpus": [], "ram_gb": 16, "cpu_cores": 8, "platform": "Linux"}
PROFILE_8GB = {"gpus": [{"name": "RTX 3070", "vram_gb": 8}], "ram_gb": 32,
               "cpu_cores": 12, "platform": "Linux"}


def _src(**kw):
    base = {"hardware": "auto", "tier": "auto", "model": "auto", "plugins": "auto"}
    base.update(kw)
    return Source.from_dict(base)


# --- gate (3): renders both a big-GPU profile and a mocked CPU-only profile into valid configs
def test_5090_profile_picks_ultra():
    rc = render(_src(hardware=PROFILE_5090), CATALOG)
    assert rc.model.id == "huihui-qwen3.6-27b-q6"
    assert rc.tier == "ultra"
    assert rc.env["LLAMACPP_GPU_LAYERS"] == "-1"
    assert "comfyui" in rc.plugins_enabled           # media enabled on NVIDIA
    assert rc.ctx_size >= 8192


def test_cpu_only_profile_degrades_honestly():
    rc = render(_src(hardware=PROFILE_CPU), CATALOG)
    assert rc.model.cpu_ok is True                    # a CPU-runnable model was chosen
    assert rc.env["LLAMACPP_GPU_LAYERS"] == "0"       # no GPU offload
    assert rc.plugins_enabled == []                   # no GPU → no media plugins
    assert rc.model.ram_gb <= PROFILE_CPU["ram_gb"]   # fits system RAM


def test_small_gpu_respects_headroom():
    # 8GB card, 4GB reserve => 4GB usable. 3B(3GB) fits, 7B(6GB) does not.
    rc = render(_src(hardware=PROFILE_8GB), CATALOG)
    assert rc.model.vram_gb <= 4.0
    assert rc.model.id == "qwen2.5-3b-instruct-q4"


# --- gate (4): the one ctx value is identical across every consumer (the bug that started this)
@pytest.mark.parametrize("profile", [PROFILE_5090, PROFILE_CPU, PROFILE_8GB])
def test_ctx_consistency(profile):
    rc = render(_src(hardware=profile), CATALOG)
    d = rc.manifest()["derived"]
    assert (str(d["env.LLAMACPP_CTX_SIZE"]) == str(d["hermes.context_length"])
            == str(d["model_gateway.ctx"]))


# --- gate (1) + (2): render from one source, and drift is corrected on re-render
def test_render_writes_and_drift_reverts(tmp_path):
    src = _src(hardware=PROFILE_5090)
    render(src, CATALOG).write(tmp_path)
    env = (tmp_path / ".env").read_text()
    assert "LLAMACPP_CTX_SIZE=" in env
    good_ctx = json.loads((tmp_path / "manifest.json").read_text())["ctx_size"]

    # hand-edit a DERIVED output to a wrong value (simulate drift)
    drifted = env.replace(f"LLAMACPP_CTX_SIZE={good_ctx}", "LLAMACPP_CTX_SIZE=999999")
    (tmp_path / ".env").write_text(drifted)
    assert "999999" in (tmp_path / ".env").read_text()

    # re-render from the (unchanged) source -> the hand-edit is overwritten
    render(src, CATALOG).write(tmp_path)
    assert "999999" not in (tmp_path / ".env").read_text()
    assert f"LLAMACPP_CTX_SIZE={good_ctx}" in (tmp_path / ".env").read_text()


def test_override_survives_regeneration_and_stays_consistent():
    src = _src(hardware=PROFILE_5090, overrides={"llamacpp": {"ctx_size": 65536}})
    rc = render(src, CATALOG)
    assert rc.ctx_size == 65536
    d = rc.manifest()["derived"]
    # override flows to ALL consumers, not just one (no new drift)
    assert str(d["env.LLAMACPP_CTX_SIZE"]) == str(d["hermes.context_length"]) == "65536"


def test_forced_model_too_big_warns_but_allows():
    src = _src(hardware=PROFILE_8GB, model="huihui-qwen3.6-27b-q6")
    rc = render(src, CATALOG)
    assert rc.model.id == "huihui-qwen3.6-27b-q6"     # honored
    assert any("VRAM" in w for w in rc.warnings)      # but warned


def test_catalog_entries_have_requirements():
    assert CATALOG.models, "catalog is empty"
    for m in CATALOG.models:
        assert m.ctx_default > 0 and m.tier
