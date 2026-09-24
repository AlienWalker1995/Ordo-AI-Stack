"""`ordo doctor` — a one-command sanitized support bundle.

When a stranger's install misbehaves (or silently degrades), this exports everything needed to
debug it into an issue: hardware profile, what the sizer chose, the rendered config (with any
secret-ish values redacted), catalog integrity, and plugin availability. No secrets leave the box.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from . import bringup, substrate
from .catalog import Catalog
from .config import Source
from .plugins import PluginRegistry
from .render import render

_SECRET_KEY = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)

# Runs inside the ops-controller container: /health is unauthenticated, so no token is needed.
_HEALTH_SCRIPT = (
    "import json, urllib.request\n"
    "print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=15))))\n"
)


class SubstrateUnreadable(Exception):
    """ops-controller's substrate digest could not be read."""


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


def read_running_substrate_digest(project: str) -> str | None:
    """The running ops-controller's substrate digest from its /health.

    None when no ops-controller is running; "" when its image predates the digest. Raises
    SubstrateUnreadable when docker or the container cannot be queried.
    """
    try:
        container = bringup.find_ops_controller(project)
    except bringup.LeaseUnknown as e:
        raise SubstrateUnreadable(str(e)) from e
    if container is None:
        return None
    try:
        proc = subprocess.run(["docker", "exec", container, "python", "-c", _HEALTH_SCRIPT],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise SubstrateUnreadable(f"{container}: /health could not be read: {e}") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise SubstrateUnreadable(f"{container}: /health could not be read: "
                                  f"{detail[-1] if detail else f'exit {proc.returncode}'}")
    try:
        health = json.loads(proc.stdout)
    except ValueError as e:
        raise SubstrateUnreadable(f"{container}: unreadable /health: {proc.stdout.strip()[:200]}") from e
    return str(health.get("substrate_digest") or "") if isinstance(health, dict) else ""


def substrate_check(project: str) -> tuple[bool, str]:
    """(ok, one-line report) comparing the running ops-controller's substrate with this checkout's."""
    checkout = substrate.current_digest()
    try:
        running = read_running_substrate_digest(project)
    except SubstrateUnreadable as e:
        return False, f"! substrate: cannot read the running ops-controller's digest ({e})"
    if running is None:
        return True, f"substrate: checkout {checkout[:12]}; ops-controller not running"
    if running == checkout:
        return True, f"substrate: ops-controller matches this checkout ({checkout[:12]})"
    return False, (f"! substrate MISMATCH: ops-controller {running[:12] or '(predates the digest)'} vs "
                   f"checkout {checkout[:12]}. Its next model switch or plugin toggle would render from "
                   f"different inputs (it refuses with 409 once out/ records a digest). Rebuild "
                   f"ordo/ops-controller from this checkout, then `ordo recreate ops-controller`.")
