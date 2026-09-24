"""`ordo up` / `ordo recreate`: the one sanctioned way to bring the stack up from the host.

Two things live here:

- `compose_argv`, the ONE builder of a `docker compose` invocation against the rendered stack.
  ops-controller's `DockerBackend._compose` and the host CLI both call it, so the env files and
  the profile set cannot diverge between the control plane and the operator's shell.
- The GPU lease check that runs before any host bring-up. A whole-stack `up -d` during a render
  starts the llama.cpp the scheduler evicted to make room, and two tenants on one card have
  crashed the host. So the command asks ops-controller's `/status` first and refuses when the
  bring-up would start an evicted resident.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

from . import images

COMPOSE_FILE = "docker-compose.yml"
OPS_CONTROLLER_SERVICE = "ops-controller"
# Services sharing caddy's network namespace follow caddy: recreating caddy without them leaves
# them attached to the old, dead namespace, so a caddy bring-up names them too.
NETNS_OWNER = "caddy"

# Runs inside the ops-controller container, which holds its own token and serves on loopback.
_STATUS_SCRIPT = (
    "import json, os, urllib.request\n"
    "req = urllib.request.Request('http://127.0.0.1:9000/status',\n"
    "    headers={'Authorization': 'Bearer ' + os.environ['OPS_CONTROLLER_TOKEN']})\n"
    "print(json.dumps(json.load(urllib.request.urlopen(req, timeout=15))['gpu']))\n"
)


class LeaseUnknown(Exception):
    """ops-controller exists but its lease state could not be read. Callers must refuse."""


def compose_argv(compose_dir: str, project: str, *args: str, profiles: Sequence[str] = ()) -> list[str]:
    """`docker compose` against the rendered stack in `compose_dir`, followed by `args`.

    `profiles` widens the resolvable set so a target whose `depends_on:` names a profiled service
    resolves (without it `docker compose ... open-webui` aborts with "no such service: qdrant").
    Widening is safe; `--no-deps` is what limits a start to the named services.

    BOTH env files, always. Passing any --env-file disables compose's implicit .env auto-load, so
    .env must be listed too; without secrets.env every ${LITELLM_MASTER_KEY} style reference goes
    UNSET and secret-dependent services crash-loop (the 2026-06-26 oauth2-proxy 11-byte-cookie
    outage). Order matters: derived first, secrets second.
    """
    cmd = ["docker", "compose", "-p", project, "-f", f"{compose_dir}/{COMPOSE_FILE}"]
    for profile in profiles:
        cmd += ["--profile", profile]
    cmd += [
        "--env-file", f"{compose_dir}/.env",
        "--env-file", f"{compose_dir}/secrets.env",
    ]
    return cmd + list(args)


def load_compose(compose_dir: str) -> dict:
    """The rendered compose file as a dict. Raises OSError / yaml.YAMLError when unreadable."""
    with open(f"{compose_dir}/{COMPOSE_FILE}", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def profiles_in(doc: dict) -> list[str]:
    """Every profile named anywhere in the rendered compose, sorted for determinism."""
    found: set[str] = set()
    for service in (doc.get("services") or {}).values():
        for profile in (service or {}).get("profiles") or []:
            found.add(str(profile))
    return sorted(found)


def _services(doc: dict) -> dict:
    return doc.get("services") or {}


def netns_members(doc: dict, owner: str = NETNS_OWNER) -> list[str]:
    """Services declared with `network_mode: service:<owner>`, sorted."""
    return sorted(
        name for name, spec in _services(doc).items()
        if (spec or {}).get("network_mode") == f"service:{owner}"
    )


def plan_named(doc: dict, services: Sequence[str], *, force_recreate: bool) -> tuple[list[str], set[str]]:
    """(compose args, services compose will start) for a named-service bring-up.

    Named services are started alone (`--no-deps`). caddy also takes its netns members, listed by
    name so compose recreates them after caddy (it still orders named services by `depends_on`
    under `--no-deps`), while caddy's own dependencies are left alone.
    """
    targets = list(services)
    if NETNS_OWNER in targets:
        targets += [m for m in netns_members(doc) if m not in targets]
    args = ["up", "-d", "--no-deps"] + (["--force-recreate"] if force_recreate else []) + targets
    return args, set(targets)


def is_leased(gpu: dict) -> bool:
    """`leased` is the scheduler's own verdict; older images only expose the raw lists."""
    return bool(gpu.get("leased") or gpu.get("running") or gpu.get("evicted_residents"))


def _holders(gpu: dict) -> str:
    running = ", ".join(f"{job.get('id')} ({job.get('kind')})" for job in gpu.get("running") or []) or "none"
    evicted = ", ".join(sorted(gpu.get("evicted_residents") or {})) or "none"
    return f"lease held by: {running}; evicted residents: {evicted}"


def lease_refusal(gpu: dict | None, *, whole_stack: bool, starts: set[str]) -> str | None:
    """Why this bring-up must not run now, or None when it is safe."""
    if gpu is None:
        return None  # no ops-controller running (fresh install): there is no lease to honor
    if whole_stack and is_leased(gpu):
        return (f"refusing a whole-stack bring-up while the GPU is leased ({_holders(gpu)}). "
                f"It would start the evicted residents beside the running GPU work. "
                f"Wait for the lease to end, or name the services you need.")
    if OPS_CONTROLLER_SERVICE in starts and is_leased(gpu):
        return (f"refusing to recreate {OPS_CONTROLLER_SERVICE} while the GPU is leased ({_holders(gpu)}). "
                f"The scheduler's lease and eviction state live in its memory: a restart loses them, so "
                f"the evicted residents are never restored, or are restored beside the running GPU work. "
                f"Wait for the lease to end.")
    evicted = sorted(starts & set(gpu.get("evicted_residents") or {}))
    if evicted:
        return (f"refusing to start {', '.join(evicted)}: evicted by the GPU scheduler to make room "
                f"({_holders(gpu)}). The scheduler restores it when the lease ends.")
    return None


def find_ops_controller(project: str) -> str | None:
    """The running ops-controller container's name, or None when there is none.

    Raises LeaseUnknown when docker cannot be queried.
    """
    try:
        ps = subprocess.run(
            ["docker", "ps",
             "--filter", f"label=com.docker.compose.project={project}",
             "--filter", f"label=com.docker.compose.service={OPS_CONTROLLER_SERVICE}",
             "--filter", "status=running",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise LeaseUnknown(f"cannot query docker for the ops-controller container: {e}") from e
    if ps.returncode != 0:
        raise LeaseUnknown(f"cannot query docker for the ops-controller container: {ps.stderr.strip()}")
    names = ps.stdout.split()
    return names[0] if names else None


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
             with_profiles: bool, force_recreate: bool, dry_run: bool, build: bool = False) -> int:
    """Check the lease, build missing first-party images (`build`), then run (or with dry_run
    print) the compose bring-up.

    Exit codes: 0 ok, 1 bad input (no render, unknown service) or a failed image build, 2 refused
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
    unknown = [s for s in services if s not in _services(doc)]
    if unknown:
        print(f"no such service in the rendered stack: {', '.join(unknown)}", file=sys.stderr)
        return 1

    if whole_stack:
        args, starts = ["up", "-d"], set(_services(doc))
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
    refusal = lease_refusal(gpu, whole_stack=whole_stack, starts=starts)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    if build:
        # Only what this bring-up starts: a --core up leaves profiled services down, so their
        # images are not needed yet.
        needed = starts if with_profiles or not whole_stack else {
            name for name in starts if not (_services(doc)[name] or {}).get("profiles")}
        # Read by module attribute at call time, so tests can replace the build step.
        code = images.ensure_images(compose_dir, doc, sorted(needed), project=project, dry_run=dry_run)
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
