"""`ordo apply`: deploy the checkout and the operator source in one ordered command.

A deploy used to be several commands the operator had to remember and order: `ordo build`, a
render, `ordo secrets materialize`, `ordo recreate ops-controller` when ordo/ changed, `ordo up`,
`ordo doctor`. Each wrong order has bitten: a render before the build names tags that do not exist
yet, recreating services before ops-controller leaves its substrate-digest guard answering 409, and
a whole-stack recreate during a GPU lease is refused halfway through. `ordo apply` plans and runs
the one correct order:

  1. build the first-party images whose inputs changed (an existing clean tag is skipped)
  2. render out/ from the operator source (the compose pins the tags step 1 recorded)
  3. materialize secrets.env and the file secrets from the secret store
  4. compute the changed set: every long-running service whose rendered config hash or image id
     differs from its running container's
  5. check the GPU lease against that set (refused before anything is recreated)
  6. run the host preflight for the services that start
  7. recreate ops-controller first when it changed (its image, config or substrate digest), and
     wait until it answers /status again
  8. recreate the rest of the changed set (`--no-deps --force-recreate`, caddy with its netns
     members), fetching the model files they load that the models volume lacks
  9. `ordo doctor`

Config hashes are compared with one compose version: the host's compose computes the rendered
hashes and recreates the changed set, and a container another compose version created is
recreated rather than compared, because versions normalise the config differently (#237).
A netns member (`network_mode: service:caddy`) is hashed the way compose labels it: with the
reference resolved to the owner's container id (see `shared_namespace_overrides`).

Every read that decides what to recreate fails closed: an unreadable lease, container list,
compose config or substrate digest refuses the apply. `--dry-run` computes the same plan against a
render staged in a temporary directory and changes nothing.

The docker CLI and the host steps sit behind two seams (`DockerState`, and the `Host` methods
`run` calls), so the orchestration is tested without docker.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import yaml

from ..render import image_tags, secret_files, stack, substrate
from . import bringup, doctor, images

OPS_CONTROLLER = bringup.OPS_CONTROLLER_SERVICE
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
CONFIG_HASH_LABEL = "com.docker.compose.config-hash"
COMPOSE_VERSION_LABEL = "com.docker.compose.version"
ONEOFF_LABEL = "com.docker.compose.oneoff"
# The compose keys whose `service:<owner>` value compose resolves to `container:<owner id>` before
# it hashes the service (compose's convergence.resolveSharedNamespaces).
SHARED_NAMESPACE_KEYS = ("network_mode", "ipc", "pid")
SERVICE_REFERENCE_PREFIX = "service:"
# How long to wait for a recreated ops-controller to answer /status before the rest is recreated
# (every later bring-up reads the lease through it, and refuses while it cannot).
OPS_CONTROLLER_READY_SECONDS = 120.0
OPS_CONTROLLER_POLL_SECONDS = 3.0


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


@dataclasses.dataclass(frozen=True)
class Staged:
    """A render the plan reads: out/ itself, or (dry run) a temporary copy."""
    compose_dir: str             # holds docker-compose.yml, .env and secrets.env
    project_directory: str       # the out/ the running containers were created against
    doc: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class Change:
    service: str
    reasons: tuple[str, ...]

    def describe(self) -> str:
        return f"{self.service} ({'; '.join(self.reasons)})"


# --------------------------------------------------------------------------- #
# The changed set.
# --------------------------------------------------------------------------- #


def diff_services(rendered: dict[str, RenderedService], running: dict[str, RunningContainer], *,
                  compose_version: str, built_refs: frozenset[str] | set[str] = frozenset(),
                  running_substrate: str | None = None, checkout_substrate: str | None = None) -> list[Change]:
    """Every long-running rendered service that differs from its container, ops-controller first.

    `built_refs` are the image refs this apply builds (a dry run cannot read their ids yet).
    `running_substrate` is the running ops-controller's digest (None when there is none to ask);
    when it differs from `checkout_substrate` ops-controller changes even if nothing else did."""
    changes = []
    for name in sorted(rendered):
        service = rendered[name]
        if service.one_shot:
            continue
        reasons = _reasons(service, running.get(name), compose_version, built_refs)
        if name == OPS_CONTROLLER and running_substrate is not None and checkout_substrate \
                and running_substrate != checkout_substrate:
            reasons.append(f"substrate digest {running_substrate[:12] or '(none)'} -> {checkout_substrate[:12]}")
        if reasons:
            changes.append(Change(name, tuple(reasons)))
    return sorted(changes, key=lambda change: (change.service != OPS_CONTROLLER, change.service))


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
        """Each rendered service's config hash and image, computed by this host's compose through the
        same argv builder as every bring-up, against the directory the containers were created from.
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
                                              container_id=str(container.get("Id") or ""))
        return found


# --------------------------------------------------------------------------- #
# The host: every step `run` takes, wired to the real checkout, docker and CLI helpers.
# --------------------------------------------------------------------------- #


class RealHost:
    """The steps `run` takes against this machine. The preflight, secrets and doctor steps are the
    CLI's own (`ordo up`'s host checks, its secret materialize, `ordo doctor`), passed in."""

    def __init__(self, *, source_path: Path, catalog_path: Path, out: Path, project: str,
                 preflight: Callable[[list[str]], bool], materialize_secrets: Callable[[], int],
                 doctor: Callable[[], int], docker: DockerState | None = None):
        self.source_path = Path(source_path)
        self.catalog_path = Path(catalog_path)
        self.out = Path(out)
        self.project = project
        self._preflight = preflight
        self._materialize_secrets = materialize_secrets
        self._doctor = doctor
        self.docker = docker or DockerState()
        self._rendered_config = None

    def _render(self):
        """The render of the operator source, computed once (it reads the checkout, not docker)."""
        if self._rendered_config is None:
            from ..render.catalog import Catalog
            from ..render.config import Source
            from ..render.engine import render

            rendered = render(Source.load(self.source_path), Catalog.load(self.catalog_path))
            # The invariant `ordo render` checks on every render: one context size everywhere.
            derived = rendered.manifest()["derived"]
            if len({str(value) for value in derived.values()}) != 1:
                raise ValueError(f"the render's context sizes disagree ({derived}); nothing was written")
            self._rendered_config = rendered
        return self._rendered_config

    # reads
    def gpu_status(self) -> dict | None:
        return bringup.read_gpu_status(self.project)

    def rendered_doc(self) -> dict[str, Any]:
        return self._render().compose_dict(project=self.project, image_tags=image_tags.load_record(self.out))

    def planned_builds(self, only: Sequence[str] | None) -> list[images.PlannedBuild]:
        scope = None if only is None else sorted({OPS_CONTROLLER, *only})
        targets = images.build_targets(self.rendered_doc(), scope, project=self.project, skip_upstream=True)
        return images.plan_builds(targets, git=images.Git(), docker=images.Docker())

    @contextlib.contextmanager
    def staged_render(self, builds: Sequence[images.PlannedBuild], *, dry_run: bool) -> Iterator[Staged]:
        out = self.out.resolve().as_posix()
        if not dry_run:
            self._render().write(self.out)
            print(f"rendered -> {self.out}/ from {self.source_path}")
            yield Staged(compose_dir=out, project_directory=out, doc=stack.load_compose(out))
            return
        # The dry run renders what this apply would write into a temporary directory: the tags the
        # builds would record, and the secrets.env and file-secret digests out/ holds now (the
        # materialize step is skipped).
        planned = {b.target.image: b.tag for b in builds}
        with tempfile.TemporaryDirectory(prefix="ordo-apply-") as tmp:
            image_tags.save_record(tmp, {**image_tags.load_record(self.out), **planned})
            self._render().write(tmp)
            for name in ("secrets.env", secret_files.DIGESTS_ENV_FILE):
                current = self.out / name
                if current.exists():
                    shutil.copyfile(current, Path(tmp) / name)
                else:
                    (Path(tmp) / name).write_text("", encoding="utf-8")
            staged_dir = Path(tmp).as_posix()
            yield Staged(compose_dir=staged_dir, project_directory=out, doc=stack.load_compose(staged_dir))

    def rendered_services(self, staged: Staged,
                          containers: dict[str, RunningContainer]) -> dict[str, RenderedService]:
        return self.docker.rendered(staged, project=self.project, profiles=stack.profiles_in(staged.doc),
                                    containers=containers)

    def running_containers(self) -> dict[str, RunningContainer]:
        return self.docker.running(project=self.project)

    def compose_version(self) -> str:
        return self.docker.compose_version()

    def running_substrate(self) -> str | None:
        return doctor.read_running_substrate_digest(self.project)

    def checkout_substrate(self) -> str:
        return substrate.current_digest()

    # mutations
    def build(self, builds: Sequence[images.PlannedBuild]) -> int:
        return images.build_images([b.target for b in builds], git=images.Git(), docker=images.Docker(),
                                   out_dir=self.out)

    def materialize_secrets(self) -> int:
        return self._materialize_secrets()

    def preflight(self, services: list[str]) -> bool:
        return self._preflight(services)

    def bring_up(self, services: Sequence[str], *, fetch_models: bool) -> int:
        return bringup.bring_up(str(self.out), self.project, list(services), whole_stack=False,
                                with_profiles=True, force_recreate=True, dry_run=False, build=True,
                                models_catalog=self.catalog_path if fetch_models else None)

    def wait_for_ops_controller(self) -> bool:  # pragma: no cover - polls docker
        deadline = time.monotonic() + OPS_CONTROLLER_READY_SECONDS
        while True:
            try:
                if bringup.read_gpu_status(self.project) is not None:
                    return True
            except bringup.LeaseUnknown:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(OPS_CONTROLLER_POLL_SECONDS)

    def doctor(self) -> int:
        return self._doctor()


# --------------------------------------------------------------------------- #
# The plan and its execution.
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Plan:
    builds: list[images.PlannedBuild]
    changes: list[Change]              # what this apply recreates, ops-controller first
    starts: list[str]                  # the changes plus the netns members that follow them
    left_out: list[str]                # changed, but outside --only
    one_shots: list[str]               # changed one-shot jobs, never started by apply
    orphans: list[str]                 # containers the render no longer defines (left alone)
    gpu: dict | None
    refusal: str | None

    @property
    def ops_controller(self) -> Change | None:
        return next((c for c in self.changes if c.service == OPS_CONTROLLER), None)

    @property
    def others(self) -> list[Change]:
        return [c for c in self.changes if c.service != OPS_CONTROLLER]


def _lease_line(gpu: dict | None) -> str:
    if gpu is None:
        return "no ops-controller running (no lease to honor)"
    if not bringup.is_leased(gpu):
        return "not held"
    running = ", ".join(str(job.get("id")) for job in gpu.get("running") or []) or "none"
    evicted = ", ".join(sorted(gpu.get("evicted_residents") or {})) or "none"
    return f"HELD by {running}; evicted residents: {evicted}"


def describe(plan: Plan, *, host: Any, dry_run: bool) -> str:
    to_build = [b for b in plan.builds if b.needs_build]
    lines = [f"ordo apply plan{' (dry run: nothing is built, written or started)' if dry_run else ''}",
             f"  GPU lease: {_lease_line(plan.gpu)}",
             f"  1. build      {len(to_build)} first-party image(s) to build, "
             f"{len(plan.builds) - len(to_build)} up to date"]
    lines += [f"                 build {b.ref}{'  (uncommitted changes)' if b.dirty else ''}" for b in to_build]
    lines.append(f"  2. render     {Path(host.out).as_posix()}/ from {Path(host.source_path).as_posix()}"
                 + ("  (staged in a temporary directory)" if dry_run else ""))
    lines.append("  3. secrets    materialize secrets.env and the file secrets from the secret store"
                 + ("  (compared with the secrets.env out/ holds now)" if dry_run else ""))
    if not plan.changes:
        lines.append("  4. nothing to recreate: every rendered service matches its container")
    else:
        lines.append(f"  4. preflight  host checks for the {len(plan.starts)} service(s) that start")
        step = 5
        if plan.ops_controller:
            lines.append(f"  {step}. recreate {OPS_CONTROLLER} first: {'; '.join(plan.ops_controller.reasons)}; "
                         "then wait for its /status")
            step += 1
        if plan.others:
            lines.append(f"  {step}. recreate (--no-deps --force-recreate, GPU-lease checked):")
            followers = [s for s in plan.starts if s not in {c.service for c in plan.changes}]
            lines += [f"       {c.describe()}" for c in plan.others]
            if followers:
                lines.append(f"       + netns members recreated with their owner: {', '.join(followers)}")
            lines.append("       fetching the model files they load that the models volume lacks")
            step += 1
    lines.append("  last: ordo doctor")
    notes = []
    if plan.left_out:
        notes.append(f"changed but outside --only (not recreated): {', '.join(plan.left_out)}")
    if plan.one_shots:
        notes.append(f"changed one-shot jobs (apply never starts a job): {', '.join(plan.one_shots)}")
    if plan.orphans:
        notes.append(f"containers the render no longer defines (left alone): {', '.join(plan.orphans)}")
    if notes:
        lines.append("  notes:")
        lines += [f"    {note}" for note in notes]
    return "\n".join(lines)


def _plan(host: Any, staged: Staged, builds: list[images.PlannedBuild], only: Sequence[str] | None,
          gpu: dict | None) -> Plan:
    """Read the rendered and running state and decide. Raises StateUnknown / SubstrateUnreadable."""
    running = host.running_containers()
    rendered = host.rendered_services(staged, running)
    version = host.compose_version()
    running_substrate = host.running_substrate()
    built_refs = {b.ref for b in builds if b.needs_build}
    changes = diff_services(rendered, running, compose_version=version, built_refs=built_refs,
                            running_substrate=running_substrate, checkout_substrate=host.checkout_substrate())
    # One-shot jobs are compared as if long-running, only to report them.
    jobs = {name: dataclasses.replace(s, one_shot=False) for name, s in rendered.items() if s.one_shot}
    one_shots = [c.service for c in diff_services(jobs, running, compose_version=version, built_refs=built_refs)]
    scope = None if only is None else {OPS_CONTROLLER, *only}
    selected = [c for c in changes if scope is None or c.service in scope]
    left_out = [c.service for c in changes if c not in selected]
    args, targets = stack.plan_named(staged.doc, [c.service for c in selected], force_recreate=True)
    starts = [arg for arg in args if arg in targets]
    refusal = bringup.lease_refusal(gpu, whole_stack=False, starts=set(starts),
                                    replacement_loads_state=bringup.loads_scheduler_state(staged.doc))
    return Plan(builds=builds, changes=selected, starts=starts, left_out=left_out, one_shots=one_shots,
                orphans=sorted(set(running) - set(rendered)), gpu=gpu, refusal=refusal)


def run(host: Any, *, only: Sequence[str] | None, dry_run: bool) -> int:
    """Plan the deploy and (unless `dry_run`) execute it. 0 ok; 1 an error or a failed step; 2
    refused (the GPU lease, or state that could not be read); otherwise a bring-up's own code."""
    try:
        gpu = host.gpu_status()
    except bringup.LeaseUnknown as e:
        print(f"refusing: {e}. The GPU lease state is unknown, so a recreate could start an evicted "
              "resident beside running GPU work.", file=sys.stderr)
        return 2
    try:
        doc = host.rendered_doc()
        unknown = [name for name in only or () if name not in (doc.get("services") or {})]
        if unknown:
            print(f"no such service in the rendered stack: {', '.join(unknown)}", file=sys.stderr)
            return 1
        builds = host.planned_builds(only)
    except (ValueError, RuntimeError) as e:
        print(f"ordo apply: {e}", file=sys.stderr)
        return 1

    if not dry_run:
        code = host.build(builds)
        if code:
            return code
    with host.staged_render(builds, dry_run=dry_run) as staged:
        if not dry_run:
            code = host.materialize_secrets()
            if code:
                return code
        try:
            plan = _plan(host, staged, builds, only, gpu)
        except (StateUnknown, doctor.SubstrateUnreadable) as e:
            print(f"refusing: {e}. What is running is unknown, so the changed set cannot be computed.",
                  file=sys.stderr)
            return 2

    print(describe(plan, host=host, dry_run=dry_run))
    if plan.refusal:
        print(plan.refusal, file=sys.stderr)
        return 2
    if dry_run:
        return 0
    if plan.changes:
        if not host.preflight(plan.starts):
            return 1
        if plan.ops_controller:
            code = host.bring_up([OPS_CONTROLLER], fetch_models=False)
            if code:
                return code
            if not host.wait_for_ops_controller():
                print(f"{OPS_CONTROLLER} did not answer /status within {OPS_CONTROLLER_READY_SECONDS:.0f}s; "
                      "the rest of the changed set was not recreated. Check `docker logs` for it, then "
                      "re-run ordo apply.", file=sys.stderr)
                return 1
        if plan.others:
            code = host.bring_up([c.service for c in plan.others], fetch_models=True)
            if code:
                return code
    return host.doctor()
