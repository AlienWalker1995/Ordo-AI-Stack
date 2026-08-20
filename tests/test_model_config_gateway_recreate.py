"""The model-config apply path must recreate model-gateway whenever a key the
gateway's entrypoint templates into its LiteLLM config changes — not only ctx.

Regression: a model swap without a ctx change recreated llamacpp only, leaving the
gateway advertising the OLD model (stale pin-alias names, weights_file, vision flag)
— violating its 'cannot drift from what's running' contract and mis-attributing
throughput samples. llamacpp_flags is a pure module (stdlib re only), so it loads
by file path like the other ops-api unit targets."""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "llamacpp_flags_under_test", REPO / "services" / "ops-api" / "llamacpp_flags.py"
)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)


def test_gateway_keys_cover_everything_the_entrypoint_templates():
    assert set(lf.GATEWAY_CONSUMED_KEYS) == {
        "LLAMACPP_MODEL", "LLAMACPP_CTX_SIZE", "LLAMACPP_N_PREDICT", "LLAMACPP_MMPROJ",
    }


def test_model_swap_requires_gateway_recreate():
    prev = {"LLAMACPP_MODEL": "old.gguf", "LLAMACPP_CTX_SIZE": "131072"}
    new = {"LLAMACPP_MODEL": "new.gguf", "LLAMACPP_CTX_SIZE": "131072"}
    assert lf.gateway_recreate_needed(prev, new) is True


def test_ctx_change_requires_gateway_recreate():
    prev = {"LLAMACPP_MODEL": "m.gguf", "LLAMACPP_CTX_SIZE": "131072"}
    new = {"LLAMACPP_MODEL": "m.gguf", "LLAMACPP_CTX_SIZE": "65536"}
    assert lf.gateway_recreate_needed(prev, new) is True


def test_mmproj_toggle_requires_gateway_recreate():
    prev = {"LLAMACPP_MODEL": "m.gguf"}
    new = {"LLAMACPP_MODEL": "m.gguf", "LLAMACPP_MMPROJ": "/models/mm.gguf"}
    assert lf.gateway_recreate_needed(prev, new) is True


def test_unrelated_flag_change_leaves_gateway_alone():
    prev = {"LLAMACPP_MODEL": "m.gguf", "LLAMACPP_FLASH_ATTN": "auto"}
    new = {"LLAMACPP_MODEL": "m.gguf", "LLAMACPP_FLASH_ATTN": "on"}
    assert lf.gateway_recreate_needed(prev, new) is False


def test_touched_but_unchanged_value_is_not_a_change():
    prev = {"LLAMACPP_MODEL": "m.gguf", "LLAMACPP_CTX_SIZE": "131072"}
    assert lf.gateway_recreate_needed(prev, dict(prev)) is False


def test_missing_vs_empty_are_equivalent():
    assert lf.gateway_recreate_needed(
        {"LLAMACPP_MODEL": "m.gguf"},
        {"LLAMACPP_MODEL": "m.gguf", "LLAMACPP_MMPROJ": ""},
    ) is False
