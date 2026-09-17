#!/usr/bin/env python3
"""Append render-selected logging callbacks to the templated LiteLLM proxy config.

`litellm_config.yaml` carries the callbacks every deployment runs (throughput + prometheus). A
callback that only makes sense when an optional plugin is enabled (today: `langfuse_otel`, when
the `langfuse` plugin is on) is NOT written into the template: the renderer decides, and says so by
setting `LITELLM_EXTRA_CALLBACKS` (comma-separated) on the model-gateway service
(ordo/compose.py::_model_gateway). The entrypoint then calls this after the template and the MCP
merge. An empty or unset value is a no-op, so a stack without the plugin boots on the template
alone and nothing here can require the plugin.

Tracing is fail-open: a callback whose credentials are missing is SKIPPED with a warning instead of
failing the gateway, because an observability misconfiguration must never take chat down. For
`langfuse_otel` that matters twice over: without keys LiteLLM falls back to a console exporter and
would print every prompt into the container log.

Kept as a pure function + CLI (like merge_mcp_config.py) so the substrate tests run it without
LiteLLM.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

import yaml

# Env vars a callback cannot work without. A callback listed here is skipped (with a warning) when
# any of them is empty; a callback not listed has no preconditions we know how to check.
REQUIRED_ENV: dict[str, tuple[str, ...]] = {
    "langfuse_otel": ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_OTEL_HOST"),
}


def parse_callback_names(raw: str) -> list[str]:
    """`"a, b,,a"` -> `["a", "b"]`: trimmed, empty entries dropped, first occurrence wins."""
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    return names


def usable_callbacks(names: list[str], env: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """Split `names` into (usable, warnings) by checking each callback's required env vars."""
    usable: list[str] = []
    warnings: list[str] = []
    for name in names:
        missing = [var for var in REQUIRED_ENV.get(name, ()) if not str(env.get(var, "") or "").strip()]
        if missing:
            warnings.append(f"skipping callback {name!r}: {', '.join(missing)} empty or unset")
            continue
        usable.append(name)
    return usable, warnings


def add_callbacks(config_text: str, names: list[str]) -> str:
    """Return `config_text` with `names` appended to `litellm_settings.callbacks` (no duplicates)."""
    config = yaml.safe_load(config_text) or {}
    settings = config.setdefault("litellm_settings", {}) or {}
    if not isinstance(settings, dict):
        raise ValueError("`litellm_settings` must be a mapping")
    config["litellm_settings"] = settings
    callbacks = settings.get("callbacks") or []
    if not isinstance(callbacks, list):
        raise ValueError("`litellm_settings.callbacks` must be a list")
    for name in names:
        if name not in callbacks:
            callbacks.append(name)
    settings["callbacks"] = callbacks
    return yaml.safe_dump(config, sort_keys=False)


def main(argv: list[str], env: Mapping[str, str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: add_callbacks.py <config.yaml> <comma-separated callback names>\n")
        return 2
    config_path = Path(argv[0])
    names, warnings = usable_callbacks(parse_callback_names(argv[1]), env)
    for warning in warnings:
        sys.stderr.write(f"add_callbacks: {warning}\n")
    if not names:
        return 0
    try:
        merged = add_callbacks(config_path.read_text(encoding="utf-8"), names)
    except (ValueError, yaml.YAMLError) as e:
        sys.stderr.write(f"add_callbacks: cannot add {names} to {config_path}: {e}\n")
        return 2
    config_path.write_text(merged, encoding="utf-8")
    print(f"add_callbacks: enabled {', '.join(names)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
