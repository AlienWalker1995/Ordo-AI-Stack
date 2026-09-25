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


class ContainerUnreadable(Exception):
    """A running container could not be queried."""


class SubstrateUnreadable(ContainerUnreadable):
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
    if running == checkout:
        return True, f"substrate: ops-controller matches this checkout ({checkout[:12]})"
    return False, (f"! substrate MISMATCH: ops-controller {running[:12] or '(predates the digest)'} vs "
                   f"checkout {checkout[:12]}. Its next model switch or plugin toggle would render from "
                   f"different inputs (it refuses with 409 once out/ records a digest). Rebuild "
                   f"ordo/ops-controller from this checkout, then `ordo recreate ops-controller`.")


# --- Open WebUI: its declared connection authenticates against model-gateway ---
#
# Open WebUI's own /api/models needs a user session, so the check asks the question one level down,
# with exactly what Open WebUI uses (ENABLE_PERSISTENT_CONFIG=false makes that its env): can the
# scoped key in the running container list the gateway's models, and does that list hold the default
# chat model and the embedding model? It runs inside the container, so the key never leaves it; only
# status codes and model ids are printed.
OPEN_WEBUI_SERVICE = "open-webui"
OPEN_WEBUI_PROBE = """
import json, os, urllib.error, urllib.request

def list_models(base_env, key_env):
    url = os.environ.get(base_env, '').rstrip('/') + '/models'
    headers = {'Authorization': 'Bearer ' + os.environ.get(key_env, '')}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
            return {'status': response.status, 'models': [m.get('id') for m in json.load(response).get('data', [])]}
    except urllib.error.HTTPError as e:
        return {'status': e.code, 'models': []}
    except Exception as e:
        return {'status': 0, 'error': type(e).__name__ + ': ' + str(e)[:200], 'models': []}

report = {
    'persistent_config': os.environ.get('ENABLE_PERSISTENT_CONFIG', 'true'),
    'default_model': os.environ.get('DEFAULT_MODELS', ''),
    'embed_model': os.environ.get('RAG_EMBEDDING_MODEL', ''),
    'chat': list_models('OPENAI_API_BASE_URL', 'OPENAI_API_KEY'),
    'rag': list_models('RAG_OPENAI_API_BASE_URL', 'RAG_OPENAI_API_KEY'),
}
print(json.dumps(report))
"""


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


def open_webui_verdict(probe: dict | None) -> tuple[bool, str]:
    """(ok, one-line report) for an open-webui probe report (None: not running)."""
    if probe is None:
        return True, "open-webui: not running"
    problems = []
    if str(probe.get("persistent_config", "")).lower() != "false":
        problems.append("ENABLE_PERSISTENT_CONFIG is not false, so webui.db (not the render) holds its "
                        "connection; `ordo apply --only open-webui`")
    wanted = {"chat": ("default chat model", str(probe.get("default_model") or "")),
              "rag": ("embedding model", str(probe.get("embed_model") or ""))}
    for connection, (role, model) in wanted.items():
        result = probe.get(connection) or {}
        status = result.get("status")
        if status != 200:
            detail = result.get("error") or f"HTTP {status}"
            problems.append(f"model-gateway refuses the {connection} connection's scoped key ({detail}); "
                            f"check LITELLM_KEY_OPEN_WEBUI, then `ordo apply --only open-webui`")
        elif model not in (result.get("models") or []):
            available = ", ".join(result.get("models") or []) or "none"
            problems.append(f"the {role} {model!r} is not among the models its key may use ({available})")
    if problems:
        return False, "! open-webui: " + "; ".join(problems)
    return True, (f"open-webui: env-authoritative config; its scoped key lists "
                  f"{probe['default_model']} and {probe['embed_model']} on model-gateway")


def open_webui_check(project: str) -> tuple[bool, str]:
    """(ok, one-line report) for the running open-webui container."""
    try:
        probe = read_open_webui_probe(project)
    except ContainerUnreadable as e:
        return False, f"! open-webui: cannot probe the running container ({e})"
    return open_webui_verdict(probe)
