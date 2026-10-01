"""The NInfer backend: a catalog model whose `backend: ninfer` the render serves from the same single GPU
chat service (`llamacpp`) as every llama.cpp model, so the gateway, the scheduler, the dashboard and
Hermes keep one contract. What changes with the backend is only what the service runs: the image, the
launcher and the flags. Rolling back is a one-line `model:` change."""
from __future__ import annotations

from pathlib import Path

import pytest

from ordo.render import compose as compose_mod
from ordo.render.catalog import Catalog, Model
from ordo.render.config import Source
from ordo.render.engine import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
NINFER = "qwen3.8-27b-nvfp4-ninfer"
SWIFT = "swift-1.5-qwen3.8-flash-next-iq2xs"
TURBO = "qwen3.8-27b-turbo-fable-q6"
PROFILE_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 31.8, "compute_cap": "12.0", "uuid": "GPU-aaaa"}],
                "ram_gb": 102, "cpu_cores": 48, "platform": "Linux"}


def _render(model: str, **extra):
    return render(Source.from_dict({"hardware": PROFILE_5090, "tier": "auto", "model": model, "plugins": "auto",
                                    **extra}), CATALOG)


def _entry(**extra) -> dict:
    base = {"id": "n", "file": "m.ninfer", "source": "https://example.invalid/m.ninfer", "sha256": "a" * 64,
            "requires": {"vram_gb": 25}, "tier": "ultra", "backend": "ninfer", "backend_image": "ordo/ninfer"}
    base.update(extra)
    return base


# --- the catalog -------------------------------------------------------------------------------------------

def test_an_unknown_backend_is_refused():
    with pytest.raises(ValueError, match="backend"):
        Model.from_dict(_entry(backend="vllm"))


def test_a_ninfer_model_must_name_its_engine_image():
    with pytest.raises(ValueError, match="backend_image"):
        Model.from_dict(_entry(backend_image=None))


def test_vision_options_belong_to_the_ninfer_backend():
    model = Model.from_dict(_entry(vision={"enabled": False, "max_context": 200000}))
    assert model.vision is False and model.vision_max_context == 200000
    with pytest.raises(ValueError, match="vision"):
        Model.from_dict(_entry(backend="llama.cpp", backend_image=None, vision={"enabled": True}))


def test_the_ninfer_entry_pins_the_official_v3_artifact():
    model = CATALOG.get(NINFER)
    assert model.backend == "ninfer" and model.backend_image == "ordo/ninfer"
    assert model.file == "qwen3_8_27b_nvfp4.ninfer"
    assert model.sha256 == "74d2c57145e6ff11d1d2faa79594477f9bc903a611af1fb20218189fbbb77d82"
    assert model.size_bytes == 23719715844
    assert "/resolve/a107ba1b5b0609d9ca90d5ed61439f5f5cc7d64d/" in model.source
    for flag in ("--kv-dtype fp8", "--spec mtp", "--draft-tokens 3", "--lm-head-draft", "--preserve-thinking",
                 "--max-concurrency 1"):
        assert flag in model.extra_args
    assert model.vision is False and model.vision_max_context == 200000
    assert model.auto is False


# --- the render ------------------------------------------------------------------------------------------

def test_ninfer_renders_the_same_gpu_chat_service_with_its_own_launcher():
    rc = _render(NINFER)
    llamacpp = rc.compose_dict()["services"]["llamacpp"]
    assert llamacpp["entrypoint"] == ["/bin/sh", "/llamacpp-scripts/run-ninfer-serve.sh"]
    assert "command" not in llamacpp                      # no llama.cpp --metrics flag
    assert llamacpp["image"].startswith("ordo/ninfer")
    # still pinned to the primary card, still the bind-config-labelled launcher directory
    assert any("/llamacpp-scripts" in v for v in llamacpp["volumes"])
    assert "models-gguf:/models:ro" in llamacpp["volumes"]
    assert llamacpp["environment"]["CUDA_VISIBLE_DEVICES"] == "GPU-aaaa"


def test_ninfer_sizes_the_full_262144_window():
    # The CPU fallback's window against this one is owned by the context-coupling change, not here.
    rc = _render(NINFER)
    assert rc.ctx_size == 262144
    assert rc.env["LLAMACPP_CTX_SIZE"] == "262144"
    assert rc.env["LLAMACPP_MODEL"] == "qwen3_8_27b_nvfp4.ninfer"
    assert "LLAMACPP_VISION" not in rc.env                # text only by default


def test_the_scheduler_footprint_leaves_room_for_the_embedder():
    rc = _render(NINFER)
    # measured: 29.3GiB on the card at 262,144 tokens with fp8 KV and MTP3
    assert rc.resident_vram_gb() == pytest.approx(29.35, abs=0.05)
    assert rc.resident_vram_gb() + 1.0 <= PROFILE_5090["gpus"][0]["vram_gb"]


def test_vision_is_an_explicit_override_that_costs_context():
    rc = _render(NINFER, overrides={"llamacpp": {"vision": True}})
    assert rc.env["LLAMACPP_VISION"] == "1"
    assert rc.ctx_size == 200000 and rc.env["LLAMACPP_CTX_SIZE"] == "200000"


def test_vision_override_on_a_llamacpp_model_is_a_render_error():
    with pytest.raises(ValueError, match="vision"):
        _render(TURBO, overrides={"llamacpp": {"vision": True}})


def test_llamacpp_models_keep_their_launcher_and_metrics():
    llamacpp = _render(SWIFT).compose_dict()["services"]["llamacpp"]
    assert llamacpp["entrypoint"] == ["/bin/sh", "/llamacpp-scripts/run-llama-server.sh"]
    assert llamacpp["command"] == [compose_mod.LLAMACPP_METRICS_ARG]


def test_the_ninfer_artifact_is_required_in_the_models_volume():
    from ordo.render.models_volume import required_model_files
    rc = _render(NINFER)
    files = {n.file for n in required_model_files(rc.compose_dict(), rc.env, ["llamacpp"]) if not n.optional}
    assert "qwen3_8_27b_nvfp4.ninfer" in files


def test_the_gateway_drops_strict_from_tools_for_ninfer_only():
    """NInfer answers a `strict: true` tool with 400 strict_tools_not_supported, and Hermes and other
    clients send strict tools, so its entry turns on the gateway's strict drop. llama.cpp accepts them."""
    assert _render(NINFER).env.get("GATEWAY_DROP_TOOL_STRICT") == "true"
    assert "GATEWAY_DROP_TOOL_STRICT" not in _render(SWIFT).env
