"""Flag-schema validation tests (pure; no FastAPI/docker).

llamacpp_flags is the single source of truth for which llama.cpp launch knobs the
dashboard/ops-controller may set and how each is validated. Drives API validation,
the env-key allowlist, and MTP<->extra_args rendering.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent / "llamacpp_flags.py"
_spec = importlib.util.spec_from_file_location("llamacpp_flags_under_test", _PATH)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)


# --- enum flags ---
def test_enum_accepts_valid():
    assert lf.validate("LLAMACPP_ROPE_SCALING", "yarn") is None
    assert lf.validate("LLAMACPP_FLASH_ATTN", "auto") is None
    assert lf.validate("LLAMACPP_KV_CACHE_TYPE_K", "q8_0") is None


def test_enum_rejects_invalid():
    assert lf.validate("LLAMACPP_ROPE_SCALING", "bogus") is not None
    assert lf.validate("LLAMACPP_FLASH_ATTN", "maybe") is not None
    # fork-only TurboQuant KV type is NOT valid on the pinned mainline build
    assert lf.validate("LLAMACPP_KV_CACHE_TYPE_K", "tbq3_0") is not None


# --- int flags with range ---
def test_int_in_range_ok():
    assert lf.validate("LLAMACPP_CTX_SIZE", "262144") is None
    assert lf.validate("LLAMACPP_CTX_SIZE", 262144) is None


def test_int_out_of_range_or_nonint():
    assert lf.validate("LLAMACPP_CTX_SIZE", "1000") is not None        # below min
    assert lf.validate("LLAMACPP_CTX_SIZE", "99999999") is not None    # above max
    assert lf.validate("LLAMACPP_CTX_SIZE", "notanint") is not None


def test_gpu_layers_allows_negative_one():
    assert lf.validate("LLAMACPP_GPU_LAYERS", "-1") is None


# --- float flag ---
def test_rope_scale_float():
    assert lf.validate("LLAMACPP_ROPE_SCALE", "2") is None
    assert lf.validate("LLAMACPP_ROPE_SCALE", "1.5") is None
    assert lf.validate("LLAMACPP_ROPE_SCALE", "0.5") is not None   # must be >= 1
    assert lf.validate("LLAMACPP_ROPE_SCALE", "abc") is not None


# --- bool flag ---
def test_bool_flag():
    assert lf.validate("LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION", "1") is None
    assert lf.validate("LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION", "0") is None
    assert lf.validate("LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION", "2") is not None


# --- override_kv format ---
def test_override_kv_format():
    assert lf.validate("LLAMACPP_OVERRIDE_KV", "") is None  # empty = unset
    assert lf.validate("LLAMACPP_OVERRIDE_KV", "qwen35moe.context_length=int:524288") is None
    assert lf.validate("LLAMACPP_OVERRIDE_KV", "garbage") is not None


# --- extra_args whitelist (anti shell-injection) ---
def test_extra_args_whitelist():
    assert lf.validate("LLAMACPP_EXTRA_ARGS", "--spec-type draft-mtp --spec-draft-n-max 2") is None
    assert lf.validate("LLAMACPP_EXTRA_ARGS", "--reasoning-format deepseek") is None
    assert lf.validate("LLAMACPP_EXTRA_ARGS", "$(evil)") is not None      # shell metachars
    assert lf.validate("LLAMACPP_EXTRA_ARGS", "a; rm -rf x") is not None  # semicolon


# --- MTP first-class flags ---
def test_mtp_n_max_range():
    assert lf.validate("MTP_N_MAX", "2") is None
    assert lf.validate("MTP_N_MAX", "0") is not None
    assert lf.validate("MTP_N_MAX", "9") is not None


def test_mtp_enabled_bool():
    assert lf.validate("MTP_ENABLED", "1") is None
    assert lf.validate("MTP_ENABLED", "nope") is not None


# --- unknown key ---
def test_unknown_key_rejected():
    assert lf.validate("LLAMACPP_NOT_A_FLAG", "x") is not None


# --- ENV_KEYS exposes the managed set ---
def test_env_keys_set():
    assert "LLAMACPP_MODEL" in lf.ENV_KEYS
    assert "LLAMACPP_CTX_SIZE" in lf.ENV_KEYS
    assert "LLAMACPP_OVERRIDE_KV" in lf.ENV_KEYS
    # MTP_* are virtual (rendered into EXTRA_ARGS), not raw env keys
    assert "MTP_ENABLED" not in lf.ENV_KEYS


# --- validate_all ---
def test_validate_all_reports_only_invalid():
    errs = lf.validate_all({
        "LLAMACPP_CTX_SIZE": "262144",        # ok
        "LLAMACPP_ROPE_SCALING": "bogus",     # bad
        "LLAMACPP_FLASH_ATTN": "off",         # ok
    })
    assert set(errs) == {"LLAMACPP_ROPE_SCALING"}


# --- MTP <-> extra_args round-trip ---
def test_mtp_renders_into_extra_args():
    frag = lf.mtp_to_extra_args(True, 3)
    assert "--spec-type" in frag and "draft-mtp" in frag and "3" in frag


def test_mtp_disabled_renders_empty():
    assert lf.mtp_to_extra_args(False, 2).strip() == ""


def test_mtp_parsed_from_extra_args():
    enabled, n_max = lf.parse_mtp_from_extra_args("--spec-type draft-mtp --spec-draft-n-max 4 --reasoning-format deepseek")
    assert enabled is True
    assert n_max == 4


def test_mtp_parse_absent():
    enabled, n_max = lf.parse_mtp_from_extra_args("--reasoning-format deepseek")
    assert enabled is False


# --- compute_effective (baseline + overrides) + render ---
BASELINE = {
    "LLAMACPP_MODEL": "base.gguf",
    "LLAMACPP_CTX_SIZE": "262144",
    "LLAMACPP_ROPE_SCALING": "none",
    "LLAMACPP_EXTRA_ARGS": "--reasoning-format deepseek",
    "LLAMACPP_KV_CACHE_TYPE_K": "q8_0",
}


def test_effective_override_wins():
    eff = lf.compute_effective(BASELINE, {"LLAMACPP_CTX_SIZE": "524288"})
    assert eff["LLAMACPP_CTX_SIZE"] == "524288"
    assert eff["LLAMACPP_ROPE_SCALING"] == "none"  # inherited from baseline


def test_effective_none_clears_to_baseline():
    eff = lf.compute_effective(BASELINE, {"LLAMACPP_CTX_SIZE": None})
    assert eff["LLAMACPP_CTX_SIZE"] == "262144"


def test_effective_mtp_enabled_folds_into_extra_args():
    eff = lf.compute_effective(BASELINE, {"MTP_ENABLED": "1", "MTP_N_MAX": "3"})
    ex = eff["LLAMACPP_EXTRA_ARGS"]
    assert "--reasoning-format deepseek" in ex
    assert "--spec-type draft-mtp" in ex and "--spec-draft-n-max 3" in ex


def test_effective_mtp_disabled_strips_spec_args():
    base = dict(BASELINE,
                LLAMACPP_EXTRA_ARGS="--spec-type draft-mtp --spec-draft-n-max 2 --reasoning-format deepseek")
    eff = lf.compute_effective(base, {"MTP_ENABLED": "0"})
    assert "draft-mtp" not in eff["LLAMACPP_EXTRA_ARGS"]
    assert "--reasoning-format deepseek" in eff["LLAMACPP_EXTRA_ARGS"]


def test_render_env_file_only_managed_keys():
    eff = lf.compute_effective(BASELINE, {})
    text = lf.render_env_file(eff)
    assert "LLAMACPP_CTX_SIZE=262144" in text
    assert "MTP_ENABLED" not in text  # virtual flag is not a raw env key
    parsed = dict(line.split("=", 1) for line in text.strip().splitlines()
                  if "=" in line and not line.startswith("#"))
    assert parsed["LLAMACPP_MODEL"] == "base.gguf"


def test_overrides_for_model_extracts_mtp_virtuals():
    # given an effective EXTRA_ARGS with MTP, the UI-facing view exposes the virtuals
    view = lf.flag_view({"LLAMACPP_EXTRA_ARGS": "--spec-type draft-mtp --spec-draft-n-max 4"})
    assert view["MTP_ENABLED"] == "1"
    assert view["MTP_N_MAX"] == "4"


def test_defaults_cover_every_managed_key():
    d = lf.defaults()
    assert set(d) == lf.ENV_KEYS  # one default per managed flag, no extras
    # every default value is itself valid
    assert lf.validate_all({k: v for k, v in d.items() if k != "LLAMACPP_MODEL"}) == {}


def test_reset_to_default_via_effective():
    # an override sets ctx high; clearing it (None) falls back to the default baseline
    base = lf.defaults()
    eff = lf.compute_effective(base, {"LLAMACPP_CTX_SIZE": "524288"})
    assert eff["LLAMACPP_CTX_SIZE"] == "524288"
    eff2 = lf.compute_effective(base, {"LLAMACPP_CTX_SIZE": None})
    assert eff2["LLAMACPP_CTX_SIZE"] == "262144"  # default


def test_descriptors_are_json_safe_and_cover_flags():
    import json
    desc = lf.descriptors()
    json.dumps(desc)  # must be serializable (no callables)
    keys = {d["key"] for d in desc}
    assert lf.ENV_KEYS <= keys
    assert {"MTP_ENABLED", "MTP_N_MAX"} <= keys
    rope = next(d for d in desc if d["key"] == "LLAMACPP_ROPE_SCALING")
    assert rope["choices"] == ["none", "linear", "yarn"]
    assert rope["kind"] == "enum"
    # every flag carries a non-empty help string for the UI tooltip
    assert all(d.get("help") for d in desc), [d["key"] for d in desc if not d.get("help")]
