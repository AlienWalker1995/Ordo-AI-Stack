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

# Tooltips for the dashboard flag UI — each explains the underlying concept,
# what the value controls, and the practical tradeoff (concise but educational).
HELP = {
    "LLAMACPP_MODEL":
        "The model weights llama.cpp loads. GGUF is the quantized on-disk format; the "
        "filename usually encodes the model, size, and quant level (e.g. Q4_K_M ≈ 4-bit — "
        "smaller and faster than Q6/Q8, with a little quality loss). Changing this swaps "
        "which model the stack-wide `local-chat` alias serves.",
    "LLAMACPP_CTX_SIZE":
        "The context window: the max tokens (prompt + reply) the model can attend to at once "
        "(~0.75 words per token). This is the stack-wide cap every client sees (Open WebUI, "
        "Cline, agents). Bigger = the model 'remembers' more, but the KV cache grows ~linearly "
        "with it, costing VRAM and slowing prompt processing.",
    "LLAMACPP_GPU_LAYERS":
        "A transformer model is a stack of layers; this sets how many are offloaded to the GPU "
        "(the rest run on the much slower CPU). -1 = put every layer on the GPU — fastest, but "
        "needs enough VRAM for the whole model. Lower it only when a model doesn't fully fit.",
    "LLAMACPP_ROPE_SCALING":
        "Models encode token positions with RoPE (rotary position embeddings) and are trained "
        "for a fixed 'native' context length. This stretches positions to run BEYOND that "
        "length: 'none' = native only; 'linear' = naive interpolation; 'yarn' = a smarter scheme "
        "that keeps quality much better. Only enable when you need more context than the model "
        "was trained for.",
    "LLAMACPP_ROPE_SCALE":
        "The context-extension multiplier, used with rope scaling. e.g. 2 runs at 2× the native "
        "length (256K→512K). Reaching further costs long-range accuracy, so use the smallest "
        "factor that fits your need. Ignored when rope_scaling = none.",
    "LLAMACPP_YARN_ORIG_CTX":
        "Tells YaRN the model's native (pre-extension) context length so it scales correctly — "
        "set it to the model's trained context (e.g. 262144). The effective window then ≈ this × "
        "rope_scale. 0 = unset/auto.",
    "LLAMACPP_OVERRIDE_KV":
        "Force-override a value baked into the GGUF's metadata at load time, as key=type:value "
        "(e.g. `qwen3.context_length=int:524288` to raise a declared limit). An expert escape "
        "hatch — leave empty unless you know the exact metadata key; a wrong key/type can break "
        "loading.",
    "LLAMACPP_FLASH_ATTN":
        "Flash Attention is a fused, memory-efficient attention kernel: same math/results, but "
        "faster and far lower VRAM at long context. 'auto' lets llama.cpp choose for the "
        "build/model; 'on' forces it (required when the KV cache is quantized); 'off' disables it.",
    "LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION":
        "The KV cache stores keys/values for every past token so they aren't recomputed each "
        "step — it's the dominant VRAM cost of long context. Turning this on stores it "
        "compressed (quantized), roughly halving that memory so you can fit a bigger window, for "
        "a small quality cost. Uses the KV-cache type settings below and generally needs Flash "
        "Attention on.",
    "LLAMACPP_KV_CACHE_TYPE_K":
        "Numeric format for the KEYS half of the (quantized) KV cache. q8_0 (8-bit) is the "
        "highest-quality quantized option and the safe default; q4_0/q5_* save more VRAM at some "
        "accuracy cost; f16 = unquantized (largest). Only used when KV-cache quantization is on.",
    "LLAMACPP_KV_CACHE_TYPE_V":
        "Numeric format for the VALUES half of the (quantized) KV cache. Same scale as keys: "
        "q8_0 = best quality, smaller types save more VRAM, f16 = unquantized. Only used when "
        "KV-cache quantization is on.",
    "LLAMACPP_N_PREDICT":
        "Hard upper bound on tokens generated in one response. It's a safety backstop: if a "
        "model gets stuck in a loop (e.g. never closing its reasoning), this force-stops it "
        "instead of running forever. Set high enough not to truncate legitimate answers.",
    "LLAMACPP_REASONING_BUDGET":
        "For 'thinking' models that emit a hidden <think>…</think> block before answering, this "
        "caps tokens spent reasoning per response, so a runaway chain of thought can't consume "
        "the whole budget. It relies on the model emitting a clean end-of-thinking token; "
        "N_PREDICT is the unconditional backstop.",
    "LLAMACPP_MMPROJ":
        "Path to a multimodal projector (an mmproj GGUF) that lets the model accept images, not "
        "just text — it projects vision features into the model's token space. Set it to enable "
        "vision; leave empty for text-only (saves ~1 GB VRAM). Must match the model family.",
    "LLAMACPP_PARALLEL":
        "How many requests the server handles concurrently ('slots'). Each slot reserves its own "
        "slice of the context/KV cache, so more slots = more throughput but a smaller window per "
        "request. 1 = maximum context for a single request.",
    "LLAMACPP_USE_MMAP":
        "Memory-mapping loads the model lazily via the OS page cache instead of reading it all "
        "into RAM up front. 0 = off here because Docker bind mounts (virtiofs/9p) don't reuse the "
        "page cache across restarts, so mmap gives no benefit and can slow loads. Turn on only "
        "with a native-filesystem model path.",
    "LLAMACPP_EXTRA_ARGS":
        "Raw flags passed straight to llama-server, appended after the managed ones — an escape "
        "hatch for options without a dedicated field yet. Whitespace-split into argv (not "
        "shell-evaluated). e.g. `--reasoning-format deepseek`.",
    "MTP_ENABLED":
        "Multi-Token Prediction = speculative decoding using the model's own built-in 'draft' "
        "head: it cheaply guesses several next tokens, then the full model verifies them in one "
        "pass and keeps the correct ones. Net effect ≈ 1.5–2× faster generation with identical "
        "output. Only works on models shipped with MTP weights.",
    "MTP_N_MAX":
        "How many tokens MTP drafts ahead per step (speculation depth, 1–6). More draft tokens = "
        "bigger speedups when the guesses are accepted, but wasted work when they're rejected — "
        "the sweet spot is hardware/model-dependent, so try a few values.",
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
