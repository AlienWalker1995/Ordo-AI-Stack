"""Apply PARAM_* placeholders — mirrors comfyui-mcp WorkflowManager (subset)."""

from __future__ import annotations

import copy
import os
import random
import re
from typing import Any

PLACEHOLDER_PREFIX = "PARAM_"
PLACEHOLDER_TYPE_HINTS = {
    "STR": str,
    "STRING": str,
    "TEXT": str,
    "INT": int,
    "FLOAT": float,
    "BOOL": bool,
}

_OPTIONAL_PARAMS = frozenset(
    {
        "seed",
        "width",
        "height",
        "model",
        "steps",
        "cfg",
        "sampler_name",
        "scheduler",
        "denoise",
        "negative_prompt",
        "seconds",
        "lyrics_strength",
        "duration",
        "fps",
        "frames",
    }
)

OPTIONAL_PARAM_DEFAULTS: dict[str, Any] = {
    "width": 512,
    "height": 512,
    "model": os.environ.get("COMFY_MCP_DEFAULT_MODEL", "v1-5-pruned-emaonly.ckpt"),
    "steps": 20,
    "cfg": 8.0,
    "sampler_name": "euler",
    "scheduler": "normal",
    "denoise": 1.0,
    "negative_prompt": "text, watermark",
    "seconds": 60,
    "lyrics_strength": 0.99,
    "duration": 5,
    "fps": 24,
    "frames": 121,
}


def _parse_placeholder(value: Any) -> tuple[str, type, str] | None:
    if not isinstance(value, str) or not value.startswith(PLACEHOLDER_PREFIX):
        return None
    token = value[len(PLACEHOLDER_PREFIX) :]
    annotation: type = str
    if "_" in token:
        type_candidate, remainder = token.split("_", 1)
        type_hint = PLACEHOLDER_TYPE_HINTS.get(type_candidate.upper())
        if type_hint:
            annotation = type_hint
            token = remainder
    param_name = _normalize_name(token)
    return param_name, annotation, value


def _normalize_name(raw: str) -> str:
    cleaned = [(char.lower() if char.isalnum() else "_") for char in raw.strip()]
    normalized = re.sub(r"_+", "_", "".join(cleaned)).strip("_")
    return normalized or "param"


def _coerce_value(value: Any, annotation: type) -> Any:
    try:
        if annotation is str:
            return str(value)
        if annotation is int:
            return int(float(value))
        if annotation is float:
            return float(value)
        if annotation is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "y"}
            return bool(value)
        return value
    except (ValueError, TypeError) as e:
        raise ValueError(f"Cannot convert {value!r} to {annotation.__name__}: {e}") from e


def get_optional_param_default(param_name: str, annotation: type) -> Any:
    if param_name == "seed" and annotation is int:
        return random.randint(0, 2**32 - 1)
    if param_name in OPTIONAL_PARAM_DEFAULTS:
        return _coerce_value(OPTIONAL_PARAM_DEFAULTS[param_name], annotation)
    return None


def apply_param_placeholders(workflow: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy workflow and replace PARAM_* string placeholders using params."""
    out = copy.deepcopy(workflow)
    required_missing: list[str] = []

    for node_id, node in list(out.items()):
        if not isinstance(node, dict) or str(node_id).startswith("__"):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for input_name, val in list(inputs.items()):
            parsed = _parse_placeholder(val)
            if not parsed:
                continue
            pname, ann, _ = parsed
            raw = params.get(pname)
            if raw is None:
                if pname in _OPTIONAL_PARAMS:
                    raw = get_optional_param_default(pname, ann)
                    if raw is None:
                        continue
                else:
                    required_missing.append(pname)
                    continue
            inputs[input_name] = _coerce_value(raw, ann)

    if required_missing:
        raise ValueError(f"Missing required parameters: {sorted(set(required_missing))}")
    return out
