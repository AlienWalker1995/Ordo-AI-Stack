"""Single source of truth for llama.cpp launch flags the dashboard/ops-controller
may set, and how each is validated.

Pure logic (no FastAPI/docker) so it is unit-testable and importable by both the
API layer (validation + the env-key allowlist) and the render step. The dashboard
fetches these descriptors to build its flag UI. MTP is exposed as two virtual
flags (MTP_ENABLED / MTP_N_MAX) that render into LLAMACPP_EXTRA_ARGS.
"""
from __future__ import annotations

import re

# Mainline llama.cpp KV cache types (the fork-only tbq*/tbqp* are intentionally
# excluded — they do not exist on the pinned ggml-org build).
_KV_TYPES = {"q8_0", "q4_0", "q4_1", "q5_0", "q5_1", "iq4_nl", "f16"}
# EXTRA_ARGS is word-split into argv (NOT shell-eval'd), but keep a strict
# whitelist anyway as defense-in-depth against injection via the run script.
_EXTRA_ARGS_RE = re.compile(r"^[a-zA-Z0-9 _.=:/-]*$")
_OVERRIDE_KV_RE = re.compile(r"^[\w.]+=[a-z0-9]+:.+$")


def _int(lo, hi):
    def v(val):
        try:
            n = int(str(val).strip())
        except (TypeError, ValueError):
            return "must be an integer"
        if n < lo or n > hi:
            return f"must be between {lo} and {hi}"
        return None
    return v


def _float_min(lo):
    def v(val):
        try:
            f = float(str(val).strip())
        except (TypeError, ValueError):
            return "must be a number"
        if f < lo:
            return f"must be >= {lo}"
        return None
    return v


def _enum(choices):
    def v(val):
        return None if str(val) in choices else f"must be one of {sorted(choices)}"
    return v


def _bool(val):
    return None if str(val) in {"0", "1"} else "must be 0 or 1"


def _override_kv(val):
    s = str(val).strip()
    if s == "":
        return None
    return (None if _OVERRIDE_KV_RE.match(s)
            else "must be key=type:value (e.g. arch.context_length=int:524288) or empty")


def _extra_args(val):
    return (None if _EXTRA_ARGS_RE.match(str(val))
            else "contains disallowed characters (allowed: letters, digits, space, and _ . = : / -)")


def _gguf(val):
    return None if str(val).strip().endswith(".gguf") else "must be a .gguf filename"


def _gguf_or_empty(val):
    s = str(val).strip()
    return None if s == "" or s.endswith(".gguf") else "must be a .gguf path or empty"


# key -> {group, kind, validate}. `kind` drives the UI input widget.
FLAGS = {
    "LLAMACPP_MODEL":                     {"group": "core",       "kind": "model",  "validate": _gguf},
    "LLAMACPP_CTX_SIZE":                  {"group": "core",       "kind": "int",    "validate": _int(4096, 1048576)},
    "LLAMACPP_GPU_LAYERS":                {"group": "core",       "kind": "int",    "validate": _int(-1, 1000)},
    "LLAMACPP_ROPE_SCALING":              {"group": "context",    "kind": "enum",   "validate": _enum({"none", "linear", "yarn"})},
    "LLAMACPP_ROPE_SCALE":                {"group": "context",    "kind": "float",  "validate": _float_min(1.0)},
    "LLAMACPP_YARN_ORIG_CTX":             {"group": "context",    "kind": "int",    "validate": _int(0, 1048576)},
    "LLAMACPP_OVERRIDE_KV":               {"group": "context",    "kind": "string", "validate": _override_kv},
    "LLAMACPP_FLASH_ATTN":                {"group": "attention",  "kind": "enum",   "validate": _enum({"auto", "on", "off"})},
    "LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION": {"group": "attention", "kind": "bool", "validate": _bool},
    "LLAMACPP_KV_CACHE_TYPE_K":           {"group": "attention",  "kind": "enum",   "validate": _enum(_KV_TYPES)},
    "LLAMACPP_KV_CACHE_TYPE_V":           {"group": "attention",  "kind": "enum",   "validate": _enum(_KV_TYPES)},
    "LLAMACPP_N_PREDICT":                 {"group": "gen",        "kind": "int",    "validate": _int(0, 1048576)},
    "LLAMACPP_REASONING_BUDGET":          {"group": "gen",        "kind": "int",    "validate": _int(0, 1048576)},
    "LLAMACPP_MMPROJ":                    {"group": "multimodal", "kind": "path",   "validate": _gguf_or_empty},
    "LLAMACPP_PARALLEL":                  {"group": "advanced",   "kind": "int",    "validate": _int(1, 64)},
    "LLAMACPP_USE_MMAP":                  {"group": "advanced",   "kind": "bool",   "validate": _bool},
    "LLAMACPP_EXTRA_ARGS":                {"group": "advanced",   "kind": "string", "validate": _extra_args},
}

# Virtual UI flags — rendered into LLAMACPP_EXTRA_ARGS, never written as raw env keys.
VIRTUAL = {
    "MTP_ENABLED": {"group": "mtp", "kind": "bool", "validate": _bool},
    "MTP_N_MAX":   {"group": "mtp", "kind": "int",  "validate": _int(1, 6)},
}

# The raw .env keys this module manages (excludes virtual flags).
ENV_KEYS = set(FLAGS)

# Baseline defaults (model-agnostic). effective(flag) = override if set else default.
# Per-model specifics (a 512K model's ctx/rope, vision mmproj, MTP) live as the
# model's overrides in the registry — NOT here.
DEFAULTS = {
    "LLAMACPP_MODEL": "",  # required — no sensible default; endpoint rejects empty
    "LLAMACPP_CTX_SIZE": "262144",
    "LLAMACPP_GPU_LAYERS": "-1",
    "LLAMACPP_ROPE_SCALING": "none",
    "LLAMACPP_ROPE_SCALE": "1",
    "LLAMACPP_YARN_ORIG_CTX": "0",
    "LLAMACPP_OVERRIDE_KV": "",
    "LLAMACPP_FLASH_ATTN": "auto",
    "LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION": "1",
    "LLAMACPP_KV_CACHE_TYPE_K": "q8_0",
    "LLAMACPP_KV_CACHE_TYPE_V": "q8_0",
    "LLAMACPP_N_PREDICT": "65536",
    "LLAMACPP_REASONING_BUDGET": "32768",
    "LLAMACPP_MMPROJ": "",
    "LLAMACPP_PARALLEL": "1",
    "LLAMACPP_USE_MMAP": "0",
    "LLAMACPP_EXTRA_ARGS": "--reasoning-format deepseek",
}


def defaults():
    """A fresh copy of the baseline defaults (one entry per managed env key)."""
    return dict(DEFAULTS)


# JSON-safe enum choices for the dashboard form (mirrors the validators above).
CHOICES = {
    "LLAMACPP_ROPE_SCALING": ["none", "linear", "yarn"],
    "LLAMACPP_FLASH_ATTN": ["auto", "on", "off"],
    "LLAMACPP_KV_CACHE_TYPE_K": sorted(_KV_TYPES),
    "LLAMACPP_KV_CACHE_TYPE_V": sorted(_KV_TYPES),
}

# One-line explanations surfaced as tooltips in the dashboard flag UI.
HELP = {
    "LLAMACPP_MODEL": "The GGUF weights file llama.cpp loads as the chat model.",
    "LLAMACPP_CTX_SIZE": "Context window in tokens. Stack-wide cap (Open WebUI, Cline, etc.); larger = more KV-cache VRAM.",
    "LLAMACPP_GPU_LAYERS": "How many model layers to offload to the GPU. -1 = all on GPU.",
    "LLAMACPP_ROPE_SCALING": "Method to stretch context beyond the model's native length. 'none' = native; 'yarn'/'linear' extend it.",
    "LLAMACPP_ROPE_SCALE": "Context-extension factor used with rope scaling (e.g. 2 = double the native length).",
    "LLAMACPP_YARN_ORIG_CTX": "The model's native (pre-extension) context length, for YaRN math. 0 = unset.",
    "LLAMACPP_OVERRIDE_KV": "Override a GGUF metadata key as key=type:value (e.g. raise the declared context_length). Empty = none.",
    "LLAMACPP_FLASH_ATTN": "Flash Attention. 'auto' lets llama.cpp decide; 'on' forces it (required by quantized KV cache).",
    "LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION": "Quantize the KV cache to fit longer context in VRAM (1 = on).",
    "LLAMACPP_KV_CACHE_TYPE_K": "KV-cache quantization for keys. q8_0 = best quality of the quantized set; smaller types save more VRAM.",
    "LLAMACPP_KV_CACHE_TYPE_V": "KV-cache quantization for values. q8_0 = best quality; smaller types save more VRAM.",
    "LLAMACPP_N_PREDICT": "Hard ceiling on tokens generated per request — a backstop against runaway generation.",
    "LLAMACPP_REASONING_BUDGET": "Max tokens the model may spend inside <think>…</think> per response.",
    "LLAMACPP_MMPROJ": "Vision projector (mmproj GGUF) that enables image input. Empty = text-only.",
    "LLAMACPP_PARALLEL": "Number of concurrent request slots the server handles.",
    "LLAMACPP_USE_MMAP": "Memory-map the model file. 0 = off (avoids stale page-cache on Docker bind mounts).",
    "LLAMACPP_EXTRA_ARGS": "Raw llama-server flags appended verbatim — escape hatch for anything without a dedicated field.",
    "MTP_ENABLED": "Multi-Token Prediction speculative decoding (~1.7× faster), using the model's built-in draft head.",
    "MTP_N_MAX": "Max speculative draft tokens per step (1–6). Hardware-dependent; try a few values.",
}


def descriptors():
    """JSON-safe flag metadata for the dashboard to build its form (no callables)."""
    out = []
    for key, d in {**FLAGS, **VIRTUAL}.items():
        out.append({
            "key": key,
            "group": d["group"],
            "kind": d["kind"],
            "choices": CHOICES.get(key),
            "default": DEFAULTS.get(key),
            "help": HELP.get(key),
        })
    return out


def validate(key, value):
    """Return an error string if (key, value) is invalid, else None."""
    desc = FLAGS.get(key) or VIRTUAL.get(key)
    if desc is None:
        return f"{key} is not a managed llama.cpp flag"
    return desc["validate"](value)


def validate_all(values):
    """Return {key: error} for every invalid entry (empty dict = all valid)."""
    return {k: e for k, v in values.items() if (e := validate(k, v)) is not None}


def mtp_to_extra_args(enabled, n_max):
    """Render the MTP virtual flags into the EXTRA_ARGS fragment."""
    if not enabled:
        return ""
    return f"--spec-type draft-mtp --spec-draft-n-max {int(n_max)}"


def parse_mtp_from_extra_args(extra):
    """Inverse of mtp_to_extra_args: (enabled, n_max | None)."""
    s = str(extra or "")
    if "draft-mtp" not in s:
        return (False, None)
    m = re.search(r"--spec-draft-n-max\s+(\d+)", s)
    return (True, int(m.group(1)) if m else None)


def _strip_mtp_args(extra):
    """Remove any MTP --spec-* tokens from an args string."""
    s = re.sub(r"--spec-type\s+draft-mtp\b", "", str(extra or ""))
    s = re.sub(r"--spec-draft-n-max\s+\d+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def compute_effective(baseline, overrides):
    """Merge baseline `.env` values with a model's overrides into the effective raw
    env dict.

    `overrides` may contain virtual MTP flags (folded into EXTRA_ARGS) and `None`
    values (clear -> inherit baseline). Returns only raw env keys.
    """
    eff = dict(baseline)
    for k, v in overrides.items():
        if k in VIRTUAL or v is None:
            continue
        eff[k] = str(v)

    # MTP: a structured override wins; otherwise inherit whatever EXTRA_ARGS had.
    base_enabled, base_n = parse_mtp_from_extra_args(eff.get("LLAMACPP_EXTRA_ARGS", ""))
    o_enabled = overrides.get("MTP_ENABLED")
    o_n = overrides.get("MTP_N_MAX")
    enabled = (str(o_enabled) == "1") if o_enabled is not None else base_enabled
    n_max = int(o_n) if o_n not in (None, "") else (base_n or 2)

    stripped = _strip_mtp_args(eff.get("LLAMACPP_EXTRA_ARGS", ""))
    frag = mtp_to_extra_args(enabled, n_max)
    eff["LLAMACPP_EXTRA_ARGS"] = f"{stripped} {frag}".strip() if frag else stripped
    return eff


def render_env_file(effective, header="# generated by ops-controller — do not hand-edit"):
    """Render the effective config to override-env-file text (managed keys only)."""
    lines = [header]
    for key in sorted(ENV_KEYS):
        if key in effective:
            lines.append(f"{key}={effective[key]}")
    return "\n".join(lines) + "\n"


def flag_view(effective):
    """UI-facing view: raw env values plus the derived virtual MTP flags."""
    view = {k: v for k, v in effective.items() if k in ENV_KEYS}
    enabled, n_max = parse_mtp_from_extra_args(effective.get("LLAMACPP_EXTRA_ARGS", ""))
    view["MTP_ENABLED"] = "1" if enabled else "0"
    view["MTP_N_MAX"] = str(n_max if n_max else 2)
    return view
