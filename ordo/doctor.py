"""`ordo doctor` — a one-command sanitized support bundle.

When a stranger's install misbehaves (or silently degrades), this exports everything needed to
debug it into an issue: hardware profile, what the sizer chose, the rendered config (with any
secret-ish values redacted), catalog integrity, and plugin availability. No secrets leave the box.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .config import Source
from .plugins import PluginRegistry
from .render import render

_SECRET_KEY = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)


def _sanitize_env(env: dict[str, str]) -> dict[str, str]:
    return {k: ("<redacted>" if _SECRET_KEY.search(k) else v) for k, v in env.items()}


def collect_bundle(source: Source, catalog: Catalog,
                   registry: PluginRegistry | None = None) -> dict[str, Any]:
    rc = render(source, catalog, registry)
    return {
        "hardware": rc.hardware.summary(),
        "sizing": {
            "tier": rc.tier, "model": rc.model.id,
            "ctx_size": rc.ctx_size, "warnings": rc.warnings,
        },
        "plugins_enabled": rc.plugins_enabled,
        "compose_profiles": rc.compose_profiles,
        "rendered_env": _sanitize_env(rc.env),
        "catalog": {
            "models": [m.id for m in catalog.models],
            "unpinned_sha256": [m.id for m in catalog.models if not m.sha256],
        },
    }


def write_bundle(bundle: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    return p
