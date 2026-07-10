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
    # no GPU → no media/voice plugins (monitoring is CPU-ok and remains available)
    assert not ({"comfyui", "song-gen", "voice"} & set(rc.plugins_enabled))
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


def test_site_config_flows_to_env_but_never_shadows_derived():
    # host/site keys land in .env verbatim so ${DATA_PATH}/${BASE_PATH} resolve deterministically…
    src = _src(hardware=PROFILE_5090, site={
        "DATA_PATH": "C:/dev/ordo-v2/data",
        "BASE_PATH": "C:/dev/ordo-ai-stack",
        "CODE_ROOT": "C:/dev",
        # …but a site key can NEVER shadow a derived one (drift gate): derived ctx wins.
        "LLAMACPP_CTX_SIZE": "1",
    })
    rc = render(src, CATALOG)
    assert rc.env["DATA_PATH"] == "C:/dev/ordo-v2/data"
    assert rc.env["BASE_PATH"] == "C:/dev/ordo-ai-stack"
    assert rc.env["CODE_ROOT"] == "C:/dev"
    assert rc.env["LLAMACPP_CTX_SIZE"] == str(rc.ctx_size) != "1"


def test_catalog_entries_have_requirements():
    assert CATALOG.models, "catalog is empty"
    for m in CATALOG.models:
        assert m.ctx_default > 0 and m.tier


# --- resident footprint: the value `ordo serve` registers the LLM with so a lease can evict it ---
def test_resident_vram_gb_is_weights_plus_kv_at_ctx():
    rc = render(_src(hardware=PROFILE_5090), CATALOG)
    # weights (catalog vram_gb) + KV cache at the rendered ctx (ctx * kv_kb_per_token). Must EXCEED
    # weights-only, or the scheduler would think a media job fits beside the LLM when it can't.
    kv_gb = (rc.ctx_size * rc.model.kv_kb_per_token) / (1024.0 * 1024.0)
    assert rc.resident_vram_gb() == round(rc.model.vram_gb + kv_gb, 2)
    assert rc.resident_vram_gb() > rc.model.vram_gb
    # the manifest carries it too (serve reads from the same render — no drift from what .env loads)
    assert rc.manifest()["model"]["resident_vram_gb"] == rc.resident_vram_gb()


def test_resident_footprint_forces_eviction_for_a_media_job_on_this_box():
    # regression guard for the LIVE defect: with the resident registered at its true footprint, an
    # ~18GB media job must NOT fit beside it on the 32GB card -> the lease is forced to evict the LLM.
    rc = render(_src(hardware=PROFILE_5090), CATALOG)
    free_beside_resident = rc.hardware.primary_vram_gb - rc.resident_vram_gb()
    assert free_beside_resident < 18, "resident footprint too small — a media job would wrongly co-run"


def test_resident_vram_gb_handles_missing_kv_rate():
    # a model with no kv_kb_per_token (can't estimate KV) falls back to weights-only, never crashes.
    m = next((m for m in CATALOG.models if m.kv_kb_per_token is None), None)
    if m is None:
        pytest.skip("every catalog model declares a KV rate")
    rc = render(_src(hardware=PROFILE_5090, model=m.id), CATALOG)
    assert rc.resident_vram_gb() == round(rc.model.vram_gb, 2)
