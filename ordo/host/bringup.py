"""`ordo up` / `ordo recreate`: the one sanctioned way to bring the stack up from the host.

It runs the compose invocation `compose_argv` builds (ordo/render/stack.py, the ONE builder, shared
with ops-controller's `DockerBackend._compose`), after the GPU lease check that runs before any host
bring-up. A whole-stack `up -d` during a render starts the llama.cpp the scheduler evicted to make
room, and two tenants on one card have crashed the host. So the command asks ops-controller's
`/status` first and refuses when the bring-up would start an evicted resident.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

from ..render.changed_set import OPS_CONTROLLER_SERVICE
from ..render.stack import COMPOSE_FILE, compose_argv, load_compose, plan_named, profiles_in, services_of
from . import fetch, images

# The ops-controller environment key naming its scheduler state file (ordo/render/compose.py sets it).
SCHEDULER_STATE_KEY = "SCHEDULER_STATE_PATH"

# Runs inside the ops-controller container, which holds its own token and serves on loopback. The
# token is a file (OPS_CONTROLLER_TOKEN_FILE, the rendered delivery): a `docker exec` does not see the
# serving process's environment, so the script reads the file itself, or the env var that an
# ops-controller created before file delivery still carries.
_STATUS_SCRIPT = (
    "import json, os, urllib.request\n"
    "path = os.environ.get('OPS_CONTROLLER_TOKEN_FILE', '')\n"
    "token = open(path).read().strip() if path else os.environ['OPS_CONTROLLER_TOKEN']\n"
    "req = urllib.request.Request('http://127.0.0.1:9000/status',\n"
    "    headers={'Authorization': 'Bearer ' + token})\n"
    "print(json.dumps(json.load(urllib.request.urlopen(req, timeout=15))['gpu']))\n"
)


class LeaseUnknown(Exception):
    """ops-controller exists but its lease state could not be read. Callers must refuse."""


def is_leased(gpu: dict) -> bool:
    """The scheduler's own `leased` verdict, the one definition of a leased card. A status without
    it comes from an ops-controller older than that field: refuse rather than rebuild the verdict
    here from the raw lists."""
    if "leased" not in gpu:
        raise LeaseUnknown("ops-controller reports no `leased` verdict (its image predates it); "
                           "rebuild it with `ordo apply` before bringing services up")
    return bool(gpu["leased"])


def _holders(gpu: dict) -> str:
    running = ", ".join(f"{job.get('id')} ({job.get('kind')})" for job in gpu.get("running") or []) or "none"
    evicted = ", ".join(sorted(gpu.get("evicted_residents") or {})) or "none"
    return f"lease held by: {running}; evicted residents: {evicted}"


def loads_scheduler_state(doc: dict) -> bool:
    """Whether the rendered ops-controller declares where its scheduler state lives, so a
    recreated one adopts the lease its predecessor saved (see ordo/control/scheduler_state.py)."""
    environment = (services_of(doc).get(OPS_CONTROLLER_SERVICE) or {}).get("environment") or {}
    if isinstance(environment, list):  # compose's `KEY=value` list form
        environment = dict(item.split("=", 1) for item in environment if "=" in item)
    return bool(str(environment.get(SCHEDULER_STATE_KEY) or "").strip())


def lease_refusal(gpu: dict | None, *, whole_stack: bool, starts: set[str],
                  replacement_loads_state: bool = False) -> str | None:
    """Why this bring-up must not run now, or None when it is safe.

    ops-controller may be recreated mid-lease only when the lease survives it: the running one
    reports its state saved to disk (`state_persisted`, false after a failed write), and the replacement is rendered to load it (`replacement_loads_state`).
    """
    if gpu is None:
        return None  # no ops-controller running (fresh install): there is no lease to honor
    try:
        leased = is_leased(gpu)
    except LeaseUnknown as e:
        return f"refusing: {e}"
    if whole_stack and leased:
        return (f"refusing a whole-stack bring-up while the GPU is leased ({_holders(gpu)}). "
                f"It would start the evicted residents beside the running GPU work. "
                f"Wait for the lease to end, or name the services you need.")
    if OPS_CONTROLLER_SERVICE in starts and leased:
        if gpu.get("state_persisted") is not True:
            return (f"refusing to recreate {OPS_CONTROLLER_SERVICE} while the GPU is leased ({_holders(gpu)}). "
                    f"The running {OPS_CONTROLLER_SERVICE} has not saved its lease state to disk (an older "
                    f"image, or its last write failed): a restart loses it, so the evicted residents are "
                    f"never restored, or are restored beside the running GPU work. Wait for the lease to end.")
        if not replacement_loads_state:
            return (f"refusing to recreate {OPS_CONTROLLER_SERVICE} while the GPU is leased ({_holders(gpu)}). "
                    f"The rendered {OPS_CONTROLLER_SERVICE} sets no {SCHEDULER_STATE_KEY}, so the new one "
                    f"would start without the saved lease state. Re-render, or wait for the lease to end.")
    evicted = sorted(starts & set(gpu.get("evicted_residents") or {}))
    if evicted:
        return (f"refusing to start {', '.join(evicted)}: evicted by the GPU scheduler to make room "
                f"({_holders(gpu)}). The scheduler restores it when the lease ends.")
    return None


def find_running_container(project: str, service: str) -> str | None:
    """The running container's name for a compose service, or None when there is none.

    Raises LeaseUnknown when docker cannot be queried.
    """
    try:
        ps = subprocess.run(
            ["docker", "ps",
             "--filter", f"label=com.docker.compose.project={project}",
             "--filter", f"label=com.docker.compose.service={service}",
             "--filter", "status=running",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise LeaseUnknown(f"cannot query docker for the {service} container: {e}") from e
    if ps.returncode != 0:
        raise LeaseUnknown(f"cannot query docker for the {service} container: {ps.stderr.strip()}")
    names = ps.stdout.split()
    return names[0] if names else None


def find_ops_controller(project: str) -> str | None:
    """The running ops-controller container's name, or None when there is none.

    Raises LeaseUnknown when docker cannot be queried.
    """
    return find_running_container(project, OPS_CONTROLLER_SERVICE)


def read_gpu_status(project: str) -> dict | None:
    """ops-controller's scheduler status, or None when no ops-controller container is running.

    Read by `docker exec` so the host needs neither the token nor a published port. Raises
    LeaseUnknown when docker cannot be queried or the status cannot be read (fail closed).
    """
    container = find_ops_controller(project)
    if container is None:
        return None
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "python", "-c", _STATUS_SCRIPT],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise LeaseUnknown(f"{container} is running but its /status could not be read: {e}") from e
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise LeaseUnknown(f"{container} is running but its /status could not be read: "
                           f"{detail[-1] if detail else f'exit {proc.returncode}'}")
    try:
        gpu = json.loads(proc.stdout)
    except ValueError as e:
        raise LeaseUnknown(f"{container} returned an unreadable /status: {proc.stdout.strip()[:200]}") from e
    if not isinstance(gpu, dict):
        raise LeaseUnknown(f"{container} returned an unexpected /status gpu block: {proc.stdout.strip()[:200]}")
    return gpu


def bring_up(out_dir: str, project: str, services: Sequence[str], *, whole_stack: bool,
             with_profiles: bool, force_recreate: bool, dry_run: bool, build: bool = False,
             models_catalog: str | Path | None = None) -> int:
    """Check the lease, build missing first-party images (`build`), fetch the model files the
    starting services read that the models volume lacks (`models_catalog`, the catalog pinning
    their sources; None skips the step), then run (or with dry_run print) the compose bring-up.

    Exit codes: 0 ok, 1 bad input (no render, unknown service), a failed image build or a failed
    model fetch, 2 refused
    because of the GPU lease or because the lease could not be read; otherwise compose's own exit
    code.
    """
    compose_dir = Path(out_dir).resolve().as_posix()
    try:
        doc = load_compose(compose_dir)
    except (OSError, yaml.YAMLError) as e:
        print(f"cannot read {compose_dir}/{COMPOSE_FILE} ({e}); render first: "
              f"ordo --source out/ordo.yaml render --out out", file=sys.stderr)
        return 1
    unknown = [s for s in services if s not in services_of(doc)]
    if unknown:
        print(f"no such service in the rendered stack: {', '.join(unknown)}", file=sys.stderr)
        return 1

    if whole_stack:
        args, starts = ["up", "-d"], set(services_of(doc))
    else:
        args, starts = plan_named(doc, services, force_recreate=force_recreate)
    profiles = profiles_in(doc) if with_profiles else []

    # Read by module attribute at call time, so tests can replace the reader.
    try:
        gpu = read_gpu_status(project)
    except LeaseUnknown as e:
        print(f"refusing: {e}. The GPU lease state is unknown, so a bring-up could start an "
              f"evicted resident beside running GPU work.", file=sys.stderr)
        return 2
    refusal = lease_refusal(gpu, whole_stack=whole_stack, starts=starts,
                            replacement_loads_state=loads_scheduler_state(doc))
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    # Only what this bring-up starts: a --core up leaves profiled services down, so their images
    # and model files are not needed yet.
    needed = starts if with_profiles or not whole_stack else {
        name for name in starts if not (services_of(doc)[name] or {}).get("profiles")}
    if build:
        # Read by module attribute at call time, so tests can replace the build step.
        code = images.ensure_images(compose_dir, doc, sorted(needed), project=project, dry_run=dry_run)
        if code:
            return code
    if models_catalog is not None:
        # After the build (the helper image is pulled like any upstream image), before compose
        # starts llama.cpp on an empty volume. Read by module attribute so tests can replace it.
        code = fetch.ensure_models_for_render(compose_dir, doc, sorted(needed), project=project,
                                              catalog_path=models_catalog, dry_run=dry_run)
        if code:
            return code

    cmd = compose_argv(compose_dir, project, *args, profiles=profiles)
    print(f"$ {shlex.join(cmd)}")
    if dry_run:
        return 0
    try:
        return subprocess.run(cmd).returncode
    except (OSError, subprocess.SubprocessError) as e:
        print(f"docker compose failed to run: {e}", file=sys.stderr)
        return 1
