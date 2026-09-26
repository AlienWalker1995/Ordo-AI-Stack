"""The changed set: which rendered services differ from the containers that run them.

A service is in the changed set when its rendered config hash (`docker compose config --hash`, what
compose labels a container with) or its image id differs from its container's, or when it has no
container yet. The host's `ordo apply` (ordo/host/apply.py) and ops-controller's post-render step
(`ControlPlane.apply_render`, ordo/control/api.py) both decide what to recreate from here, so the
host and the control plane cannot disagree about what a render changed. The same holds for the
stale one-shot job containers both remove (`stale_one_shot_jobs`).

Config hashes are compared with one compose version: the compose that computes the rendered hashes
is the one that recreates the changed set, and a container another compose version created is
reported as not comparable rather than compared, because versions normalise the config differently
(#237). A netns member (`network_mode: service:caddy`) is hashed the way compose labels it: with the
reference resolved to the owner's container id (see `shared_namespace_overrides`).

Every read fails closed: an unreadable container list or compose config raises StateUnknown, and the
caller refuses. The docker CLI sits behind an injected runner, so the logic is tested without docker.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from . import stack

OPS_CONTROLLER_SERVICE = "ops-controller"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
CONFIG_HASH_LABEL = "com.docker.compose.config-hash"
COMPOSE_VERSION_LABEL = "com.docker.compose.version"
ONEOFF_LABEL = "com.docker.compose.oneoff"
# The compose keys whose `service:<owner>` value compose resolves to `container:<owner id>` before
# it hashes the service (compose's convergence.resolveSharedNamespaces).
SHARED_NAMESPACE_KEYS = ("network_mode", "ipc", "pid")
SERVICE_REFERENCE_PREFIX = "service:"


class StateUnknown(Exception):
    """Docker could not tell what is rendered or running. Callers refuse: the changed set is unknown."""


@dataclasses.dataclass(frozen=True)
class RenderedService:
    service: str
    config_hash: str             # `docker compose config --hash`, what compose labels the container with
    image_ref: str               # the interpolated `image:`
    image_id: str | None         # the local image id for that ref; None when the cache lacks it
    one_shot: bool               # `restart: "no"`: a job, never started by apply


@dataclasses.dataclass(frozen=True)
class RunningContainer:
    service: str
    config_hash: str
    image_id: str
    compose_version: str         # the compose that created it (its label)
    container_id: str = ""       # the full container id (a netns member's hash names its owner's)
    state: str = ""              # `State.Status` (created, running, exited, ...); "" when unreported


@dataclasses.dataclass(frozen=True)
class Staged:
    """A render the changed set is computed against: out/ itself, or (a dry run) a temporary copy."""
    compose_dir: str             # holds docker-compose.yml, .env and secrets.env
    project_directory: str       # the directory the running containers were created against
    doc: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class StackState:
    """One read of both sides: every rendered service, every container of the project, and the
    version of the compose that computed the rendered hashes."""
    rendered: dict[str, RenderedService]
    running: dict[str, RunningContainer]
    compose_version: str


@dataclasses.dataclass(frozen=True)
class Change:
    service: str
    reasons: tuple[str, ...]
    # The container was created by another compose version, so its config hash says nothing about
    # whether the config changed (only the host's `ordo apply` recreates such a container).
    incomparable: bool = False

    def describe(self) -> str:
        return f"{self.service} ({'; '.join(self.reasons)})"


def diff_services(rendered: dict[str, RenderedService], running: dict[str, RunningContainer], *,
                  compose_version: str, built_refs: frozenset[str] | set[str] = frozenset(),
                  running_substrate: str | None = None, checkout_substrate: str | None = None) -> list[Change]:
    """Every long-running rendered service that differs from its container, ops-controller first.

    `built_refs` are the image refs the caller builds before recreating (a dry run cannot read their
    ids yet). `running_substrate` is the running ops-controller's digest (None when there is none to
    ask); when it differs from `checkout_substrate` ops-controller changes even if nothing else did."""
    changes = []
    for name in sorted(rendered):
        service = rendered[name]
        if service.one_shot:
            continue
        container = running.get(name)
        reasons = _reasons(service, container, compose_version, built_refs)
        if name == OPS_CONTROLLER_SERVICE and running_substrate is not None and checkout_substrate \
                and running_substrate != checkout_substrate:
            reasons.append(f"substrate digest {running_substrate[:12] or '(none)'} -> {checkout_substrate[:12]}")
        if reasons:
            incomparable = container is not None and container.compose_version != compose_version
            changes.append(Change(name, tuple(reasons), incomparable=incomparable))
    return sorted(changes, key=lambda change: (change.service != OPS_CONTROLLER_SERVICE, change.service))


# The container states in which nothing runs. A one-shot job's container in one of them can be
# removed without interrupting anything; anything else (running, restarting, paused, or a state
# docker did not report) might be an eval in progress, and is never removed. A service the render no
# longer defines is stopped unless it is already in one of them (a crash loop, `restarting`, is not).
STOPPED_STATES = frozenset({"created", "exited", "dead"})


@dataclasses.dataclass(frozen=True)
class StaleJob:
    """A one-shot job whose existing container differs from the render."""
    service: str
    reasons: tuple[str, ...]
    state: str                   # its container's state

    @property
    def removable(self) -> bool:
        return self.state in STOPPED_STATES

    def describe(self) -> str:
        return f"{self.service} ({'; '.join(self.reasons)})"


def stale_one_shot_jobs(rendered: dict[str, RenderedService], running: dict[str, RunningContainer], *,
                        compose_version: str,
                        built_refs: frozenset[str] | set[str] = frozenset()) -> list[StaleJob]:
    """Every one-shot job (`restart: "no"`) whose existing container differs from the render,
    compared the way a long-running service is. A job with no container is not stale: there is
    nothing to remove, and apply never creates or starts a job.

    Both executors (the host's `ordo apply` and ops-controller's `apply_render`) remove the
    removable ones, so declared config and existing containers agree after an apply. Removing a
    created or exited job container loses nothing: `docker compose run --rm` never reuses it, and
    the next run creates a fresh one from the current render."""
    stale = []
    for name in sorted(rendered):
        service = rendered[name]
        container = running.get(name)
        if not service.one_shot or container is None:
            continue
        reasons = _reasons(service, container, compose_version, built_refs)
        if reasons:
            stale.append(StaleJob(name, tuple(reasons), container.state))
    return stale


def _reasons(service: RenderedService, container: RunningContainer | None, compose_version: str,
             built_refs: frozenset[str] | set[str]) -> list[str]:
    if container is None:
        return ["not created"]
    reasons = []
    if container.compose_version != compose_version:
        reasons.append(f"created by compose v{container.compose_version}, this host runs v{compose_version}: "
                       "its config hash is not comparable")
    elif container.config_hash != service.config_hash:
        reasons.append("config changed")
    if service.image_ref in built_refs:
        reasons.append(f"image {service.image_ref} built by this apply")
    elif service.image_id is None:
        reasons.append(f"image {service.image_ref} is not in the local cache (compose pulls it)")
    elif service.image_id != container.image_id:
        reasons.append(f"image {service.image_ref} changed")
    return reasons


def shared_namespace_overrides(services: dict[str, Any],
                               containers: dict[str, RunningContainer]) -> dict[str, dict[str, str]]:
    """The compose overrides that make `config --hash` hash a namespace member the way compose does.

    When compose creates a member (`network_mode: service:caddy`), it first rewrites the reference
    to `container:<caddy container id>` and hashes (and labels) THAT config. `config --hash` hashes
    the unresolved `service:caddy`, so without this every member looks changed on every apply.
    Resolving against the owner's current container also catches the real drift: a member created
    against an older owner container (the owner was recreated without it) hashes differently.
    An owner with no container is left unresolved: the owner is itself "not created", and its
    members are recreated with it (`stack.lifecycle_group`).
    """
    overrides: dict[str, dict[str, str]] = {}
    for name, spec in sorted(services.items()):
        for key in SHARED_NAMESPACE_KEYS:
            value = str((spec or {}).get(key) or "")
            if not value.startswith(SERVICE_REFERENCE_PREFIX):
                continue
            owner = containers.get(value[len(SERVICE_REFERENCE_PREFIX):])
            if owner is not None and owner.container_id:
                overrides.setdefault(name, {})[key] = f"container:{owner.container_id}"
    return overrides


# --------------------------------------------------------------------------- #
# Reading docker.
# --------------------------------------------------------------------------- #


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run_docker(argv: list[str]) -> subprocess.CompletedProcess:  # pragma: no cover - shells out
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


class DockerState:
    """The docker CLI reads that decide the changed set. Every failure raises StateUnknown."""

    def __init__(self, run: Runner = _run_docker):
        self._run = run
        self._image_ids: dict[str, str | None] = {}

    def _ok(self, argv: list[str]) -> str:
        try:
            proc = self._run(argv)
        except (OSError, subprocess.SubprocessError) as e:
            raise StateUnknown(f"`{' '.join(argv[:4])}` failed to run: {e}") from e
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise StateUnknown(f"`{' '.join(argv[:4])}` failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
        return proc.stdout

    def compose_version(self) -> str:
        version = self._ok(["docker", "compose", "version", "--short"]).strip().lstrip("v")
        if not version:
            raise StateUnknown("`docker compose version --short` printed nothing")
        return version

    def image_id(self, ref: str) -> str | None:
        """The local image id for `ref`, None when the cache does not hold it."""
        if ref not in self._image_ids:
            argv = ["docker", "image", "inspect", "--format", "{{.Id}}", ref]
            try:
                proc = self._run(argv)
            except (OSError, subprocess.SubprocessError) as e:
                raise StateUnknown(f"`docker image inspect {ref}` failed to run: {e}") from e
            if proc.returncode == 0:
                self._image_ids[ref] = proc.stdout.strip() or None
            elif "no such image" in (proc.stderr or "").lower():
                self._image_ids[ref] = None
            else:
                raise StateUnknown(f"`docker image inspect {ref}` failed: {(proc.stderr or '').strip()}")
        return self._image_ids[ref]

    def rendered(self, staged: Staged, *, project: str, profiles: Sequence[str],
                 containers: dict[str, RunningContainer]) -> dict[str, RenderedService]:
        """Each rendered service's config hash and image, computed by this compose through the same
        argv builder as every bring-up, against the directory the containers were created from.
        `containers` (the running side) resolves the namespace references compose resolves."""
        def compose(*args: str) -> list[str]:
            return stack.compose_argv(staged.compose_dir, project, "--project-directory",
                                       staged.project_directory, *args, profiles=profiles)

        try:
            config = json.loads(self._ok(compose("config", "--format", "json")))
        except ValueError as e:
            raise StateUnknown(f"`docker compose config --format json` printed unreadable output: {e}") from e
        services = config.get("services") or {}
        overrides = shared_namespace_overrides(services, containers)
        with tempfile.TemporaryDirectory(prefix="ordo-apply-hash-") as tmp:
            override_args: list[str] = []
            if overrides:
                override_file = Path(tmp) / "namespace-overrides.yml"
                override_file.write_text(yaml.safe_dump({"services": overrides}, sort_keys=True),
                                         encoding="utf-8")
                override_args = ["-f", override_file.as_posix()]
            hash_output = self._ok(compose(*override_args, "config", "--hash", "*"))
        hashes = {}
        for line in hash_output.splitlines():
            parts = line.split()
            if len(parts) == 2:
                hashes[parts[0]] = parts[1]
        rendered = {}
        for name, config_hash in hashes.items():
            if name not in services:
                raise StateUnknown(f"`docker compose config` hashed {name} but does not define it")
            spec = services[name] or {}
            ref = str(spec.get("image") or "")
            rendered[name] = RenderedService(service=name, config_hash=config_hash, image_ref=ref,
                                             image_id=self.image_id(ref) if ref else None,
                                             one_shot=str(spec.get("restart")) == "no")
        return rendered

    def running(self, *, project: str) -> dict[str, RunningContainer]:
        """Every container of the project (running or stopped) by service. One-off `run` containers
        are skipped: they are not what `up` manages."""
        ids = self._ok(["docker", "ps", "-a", "--no-trunc", "--filter", f"label={PROJECT_LABEL}={project}",
                        "--format", "{{.ID}}"]).split()
        if not ids:
            return {}
        try:
            inspected = json.loads(self._ok(["docker", "inspect", *ids]))
        except ValueError as e:
            raise StateUnknown(f"`docker inspect` printed unreadable output: {e}") from e
        found: dict[str, RunningContainer] = {}
        for container in inspected:
            labels = (container.get("Config") or {}).get("Labels") or {}
            service = labels.get(SERVICE_LABEL)
            if not service or labels.get(ONEOFF_LABEL) == "True" or service in found:
                continue
            found[service] = RunningContainer(service=service, config_hash=labels.get(CONFIG_HASH_LABEL, ""),
                                              image_id=str(container.get("Image") or ""),
                                              compose_version=labels.get(COMPOSE_VERSION_LABEL, "").lstrip("v"),
                                              container_id=str(container.get("Id") or ""),
                                              state=str((container.get("State") or {}).get("Status") or ""))
        return found

    def read(self, staged: Staged, *, project: str) -> StackState:
        """Both sides in the order the namespace resolution needs: the containers first, then the
        rendered hashes resolved against them, with every profile the render names."""
        running = self.running(project=project)
        rendered = self.rendered(staged, project=project, profiles=stack.profiles_in(staged.doc),
                                 containers=running)
        return StackState(rendered=rendered, running=running, compose_version=self.compose_version())
