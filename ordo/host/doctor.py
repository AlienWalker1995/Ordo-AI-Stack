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

from ..render import alerting, substrate
from ..render.catalog import Catalog
from ..render.config import Source
from ..render.engine import render
from ..render.open_webui_probe import OPEN_WEBUI_PROBE, OPEN_WEBUI_SERVICE, open_webui_verdict
from ..render.plugins import PluginRegistry
from . import bringup

_SECRET_KEY = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)

# Runs inside the ops-controller container: /health is unauthenticated, so no token is needed.
_HEALTH_SCRIPT = (
    "import json, urllib.request\n"
    "print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:9000/health', timeout=15))))\n"
)


class ContainerUnreadable(Exception):
    """A running container could not be queried."""


class SubstrateUnreadable(ContainerUnreadable):
    """ops-controller's substrate digest could not be read."""


def _sanitize_env(env: dict[str, str]) -> dict[str, str]:
    return {k: ("<redacted>" if _SECRET_KEY.search(k) else v) for k, v in env.items()}


def collect_bundle(source: Source, catalog: Catalog,
                   registry: PluginRegistry | None = None, rendered: Any = None) -> dict[str, Any]:
    """`rendered`: the render of `source` when the caller already has it (it detects hardware)."""
    rc = rendered if rendered is not None else render(source, catalog, registry)
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
    health = _exec_python_json(container, _HEALTH_SCRIPT, "/health", SubstrateUnreadable)
    return str(health.get("substrate_digest") or "") if isinstance(health, dict) else ""


def _exec_python_json(container: str, script: str, what: str,
                      error: type[ContainerUnreadable] = ContainerUnreadable) -> Any:
    """Run a Python script inside a container and parse the JSON it prints; raises `error`."""
    try:
        proc = subprocess.run(["docker", "exec", container, "python", "-c", script],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise error(f"{container}: {what} could not be read: {e}") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise error(f"{container}: {what} could not be read: "
                    f"{detail[-1] if detail else f'exit {proc.returncode}'}")
    try:
        return json.loads(proc.stdout)
    except ValueError as e:
        raise error(f"{container}: unreadable {what}: {proc.stdout.strip()[:200]}") from e


def substrate_check(project: str) -> tuple[bool, str]:
    """(ok, one-line report) comparing the running ops-controller's substrate with this checkout's."""
    checkout = substrate.current_digest()
    try:
        running = read_running_substrate_digest(project)
    except SubstrateUnreadable as e:
        return False, f"! substrate: cannot read the running ops-controller's digest ({e})"
    if running is None:
        return True, f"substrate: checkout {checkout[:12]}; ops-controller not running"
    return substrate.substrate_verdict(running, checkout, reference_name="this checkout",
                                       rebuild_from="this checkout")


# --- Open WebUI: its declared connection authenticates against model-gateway ---
#
# The probe and its verdict live in ordo/render/open_webui_probe.py (ops-controller runs them too).


def read_open_webui_probe(project: str) -> dict | None:
    """The probe's report from the running open-webui container, or None when it is not running.

    Raises ContainerUnreadable when docker or the container cannot be queried.
    """
    try:
        container = bringup.find_running_container(project, OPEN_WEBUI_SERVICE)
    except bringup.LeaseUnknown as e:
        raise ContainerUnreadable(str(e)) from e
    if container is None:
        return None
    report = _exec_python_json(container, OPEN_WEBUI_PROBE, "the model-gateway probe")
    if not isinstance(report, dict):
        raise ContainerUnreadable(f"{container}: unexpected probe output: {str(report)[:200]}")
    return report


def alerting_check(compose: dict[str, Any], out_dir: str | Path) -> tuple[bool, str]:
    """(ok, one-line report): whether the rendered Alertmanager can deliver (ordo/render/alerting.py),
    judged from the materialized secret files under `out_dir`."""
    return alerting.check(compose, out_dir)


def open_webui_check(project: str) -> tuple[bool, str]:
    """(ok, one-line report) for the running open-webui container."""
    try:
        probe = read_open_webui_probe(project)
    except ContainerUnreadable as e:
        return False, f"! open-webui: cannot probe the running container ({e})"
    return open_webui_verdict(probe)


# --- The CPU fallback's thread budget: declared (overrides.llamacpp-cpu.threads) vs running ---
#
# A `docker update --cpus` on the running container changes its ceiling without touching its config
# hash, so neither the changed set nor the container labels show it. This check compares the render
# with what the container actually runs: its `--threads` argument and its CPU limit.

CPU_FALLBACK_SERVICE = "llamacpp-cpu"
_NANO_CPUS_PER_CPU = 1_000_000_000


def read_cpu_fallback_limits(project: str) -> tuple[int | None, float | None] | None:
    """(--threads, cpus limit) of the running llamacpp-cpu container, or None when it is not running.

    Either value is None when the container has none (no --threads flag; no CPU limit). Raises
    ContainerUnreadable when docker or the container cannot be queried.
    """
    try:
        container = bringup.find_running_container(project, CPU_FALLBACK_SERVICE)
    except bringup.LeaseUnknown as e:
        raise ContainerUnreadable(str(e)) from e
    if container is None:
        return None
    try:
        proc = subprocess.run(["docker", "inspect", container], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise ContainerUnreadable(f"{container}: {e}") from e
    if proc.returncode != 0:
        raise ContainerUnreadable(f"{container}: {(proc.stderr or proc.stdout).strip()[:200]}")
    try:
        details = json.loads(proc.stdout)[0]
        command = [str(arg) for arg in (details["Config"].get("Cmd") or [])]
        nano_cpus = int(details["HostConfig"].get("NanoCpus") or 0)
    except (ValueError, LookupError, TypeError, AttributeError) as e:
        raise ContainerUnreadable(f"{container}: unreadable inspect output: {e}") from e
    threads = None
    if "--threads" in command and command.index("--threads") + 1 < len(command):
        try:
            threads = int(command[command.index("--threads") + 1])
        except ValueError as e:
            raise ContainerUnreadable(f"{container}: unreadable --threads value: {e}") from e
    cpus = nano_cpus / _NANO_CPUS_PER_CPU if nano_cpus else None
    return threads, cpus


def _format_cpus(cpus: float | None) -> str:
    if cpus is None:
        return "unlimited"
    return str(int(cpus)) if cpus == int(cpus) else str(cpus)


def cpu_fallback_check(project: str, declared_threads: int | None) -> tuple[bool, str]:
    """(ok, one-line report) comparing the rendered CPU fallback budget with the running container.

    `declared_threads` is None when the render does not define llamacpp-cpu."""
    if declared_threads is None:
        return True, "cpu-fallback: not rendered"
    try:
        running = read_cpu_fallback_limits(project)
    except ContainerUnreadable as e:
        return False, f"! cpu-fallback: cannot inspect the running container ({e})"
    if running is None:
        return True, f"cpu-fallback: threads={declared_threads}, cpus={declared_threads} declared; not running"
    threads, cpus = running
    if threads == declared_threads and cpus == declared_threads:
        return True, f"cpu-fallback: threads={declared_threads}, cpus={declared_threads} (declared and running)"
    return False, (f"! cpu-fallback: declared threads={declared_threads}, cpus={declared_threads}; running "
                   f"threads={threads if threads is not None else 'unset'}, cpus={_format_cpus(cpus)} "
                   f"(recreate it: ordo apply --only {CPU_FALLBACK_SERVICE})")
