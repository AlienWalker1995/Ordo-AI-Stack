"""A catalog model's own placement: sharded weights, MoE expert offload, CPU threads, lazy mmap, its
generation cap, its VRAM reserve and whether `model: auto` may pick it.

These reach llama-server only through the render (.env keys the llamacpp launcher reads) and the
models-volume checks (every shard must be present before the chat service starts)."""
from __future__ import annotations

from pathlib import Path

import pytest

from ordo.render.catalog import Catalog, Model
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.models_volume import required_model_files

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
SWIFT = "swift-1.5-qwen3.8-flash-next-iq2xs"
TURBO = "qwen3.8-27b-turbo-fable-q6"
PROFILE_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 31.8, "compute_cap": "12.0"}], "ram_gb": 102,
                "cpu_cores": 48, "platform": "Linux"}
PLACEMENT_KEYS = ("LLAMACPP_N_CPU_MOE", "LLAMACPP_THREADS", "LLAMACPP_LOAD_MODE", "LLAMACPP_MODEL_SHARDS")


def _render(model: str):
    return render(Source.from_dict({"hardware": PROFILE_5090, "tier": "auto", "model": model, "plugins": "auto"}),
                  CATALOG)


def _entry(**extra) -> dict:
    base = {"id": "m", "file": "m-00001-of-00002.gguf", "source": "https://example.invalid/m1.gguf",
            "sha256": "a" * 64, "requires": {"vram_gb": 10}, "tier": "high"}
    base.update(extra)
    return base


# --- the catalog -------------------------------------------------------------------------------------------

def test_a_shard_is_a_downloadable_entry_of_its_own():
    model = Model.from_dict(_entry(shards=[{"file": "m-00002-of-00002.gguf", "source": "https://example.invalid/m2.gguf",
                                            "sha256": "b" * 64, "size_bytes": 5}]))
    catalog = Catalog([model])
    (shard,) = model.shards
    assert shard.id == "m-shard2" and shard.file == "m-00002-of-00002.gguf" and shard.size_bytes == 5
    assert catalog.files_of(model) == [model, shard]
    assert shard in catalog.entries()
    assert catalog.by_file("m-00002-of-00002.gguf") is shard
    assert catalog.get("m-shard2") is None          # a shard is never a chat model


def test_a_shard_must_be_pinned():
    with pytest.raises(ValueError, match="shard 2 must pin sha256"):
        Model.from_dict(_entry(shards=[{"file": "m-00002-of-00002.gguf", "source": "https://example.invalid/m2.gguf"}]))


def test_offloaded_experts_must_declare_their_cpu_threads():
    with pytest.raises(ValueError, match="cpu_threads"):
        Model.from_dict(_entry(n_cpu_moe=4))
    assert Model.from_dict(_entry(n_cpu_moe=4, cpu_threads=8)).n_cpu_moe == 4


def test_an_unknown_load_mode_is_refused():
    with pytest.raises(ValueError, match="load_mode"):
        Model.from_dict(_entry(load_mode="mlock"))


def test_a_pinnable_only_model_is_never_the_auto_pick_but_can_be_pinned():
    big = Model.from_dict(_entry(id="big", requires={"vram_gb": 20}, tier="ultra", auto=False))
    small = Model.from_dict(_entry(id="small", requires={"vram_gb": 8}, tier="high"))
    catalog = Catalog([big, small])
    rc_auto = render(Source.from_dict({"hardware": PROFILE_5090, "model": "auto", "plugins": "auto"}), catalog)
    assert rc_auto.model.id == "small"
    rc_pinned = render(Source.from_dict({"hardware": PROFILE_5090, "model": "big", "plugins": "auto"}), catalog)
    assert rc_pinned.model.id == "big"


def test_a_model_reserve_replaces_the_default_in_fit_and_context_sizing():
    tight = Model.from_dict(_entry(requires={"vram_gb": 28.6}, ctx_default=106496, kv_kb_per_token=15.4,
                                   vram_reserve_gb=1.0))
    catalog = Catalog([tight])
    rc = render(Source.from_dict({"hardware": PROFILE_5090, "model": "m", "plugins": "auto"}), catalog)
    assert rc.ctx_size == 106496
    assert not any("VRAM" in w for w in rc.warnings)
    default = Model.from_dict(_entry(requires={"vram_gb": 28.6}, ctx_default=106496, kv_kb_per_token=15.4))
    rc_default = render(Source.from_dict({"hardware": PROFILE_5090, "model": "m", "plugins": "auto"}),
                        Catalog([default]))
    assert rc_default.ctx_size == 8192             # the 4GB default reserve leaves no room for KV


# --- the Swift entry, rendered ----------------------------------------------------------------------------------

def test_swift_renders_its_measured_placement():
    rc = _render(SWIFT)
    env = rc.env
    assert env["LLAMACPP_MODEL"] == "Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ2_XS-00001-of-00002.gguf"
    assert env["LLAMACPP_MODEL_SHARDS"] == "Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ2_XS-00002-of-00002.gguf"
    assert env["LLAMACPP_N_CPU_MOE"] == "15"
    assert env["LLAMACPP_THREADS"] == "12"
    assert env["LLAMACPP_LOAD_MODE"] == "mmap-lazy"
    assert env["LLAMACPP_N_PREDICT"] == "12288"
    assert env["LLAMACPP_KV_CACHE_TYPE_K"] == env["LLAMACPP_KV_CACHE_TYPE_V"] == "q8_0"
    assert env["LLAMACPP_MMPROJ"] == ""
    assert "--temp 1.0" in env["LLAMACPP_EXTRA_ARGS"] and "--top-p 0.95" in env["LLAMACPP_EXTRA_ARGS"]
    # the window the placement was measured at, and the CPU failover accepts the same window
    assert rc.ctx_size == 106496 and env["LLAMACPP_CPU_CTX"] == env["LLAMACPP_CTX_SIZE"] == "106496"
    # the scheduler holds its true footprint: it plus the other declared resident still fits the card
    assert rc.resident_vram_gb() == pytest.approx(30.16, abs=0.01)
    assert rc.resident_vram_gb() + 1.0 <= PROFILE_5090["gpus"][0]["vram_gb"]


def test_swift_needs_both_shards_in_the_models_volume():
    rc = _render(SWIFT)
    needed = {need.file: need for need in required_model_files(rc.compose_dict(), rc.env, ["llamacpp"])}
    for shard in ("00001-of-00002", "00002-of-00002"):
        need = needed[f"Swift-Qwen3.8-Flash-Next-GSQ-RCO-IQ2_XS-{shard}.gguf"]
        assert not need.optional
    swift = CATALOG.get(SWIFT)
    assert [entry.file for entry in CATALOG.files_of(swift)] == sorted(needed)


def test_a_model_without_a_placement_renders_no_placement_keys():
    """So adding placement support changes neither the .env nor the llamacpp config hash of any
    other model: nothing recreates on merge."""
    rc = _render(TURBO)
    assert not set(PLACEMENT_KEYS) & set(rc.env)
    assert rc.env["LLAMACPP_N_PREDICT"] == "65536"
    assert not set(PLACEMENT_KEYS) & set(rc.compose_dict()["services"]["llamacpp"].get("environment") or {})


def test_swift_is_never_the_auto_pick():
    assert _render("auto").model.id == TURBO


def test_a_bad_load_mode_override_is_a_render_error():
    with pytest.raises(ValueError, match="load_mode"):
        render(Source.from_dict({"hardware": PROFILE_5090, "model": TURBO, "plugins": "auto",
                                 "overrides": {"llamacpp": {"load_mode": "mlock"}}}), CATALOG)
