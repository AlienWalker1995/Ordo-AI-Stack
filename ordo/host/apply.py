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

The changed set itself (step 4) is ordo/render/changed_set.py, shared with ops-controller's
post-render step. Config hashes are compared with one compose version: the host's compose computes
the rendered hashes and recreates the changed set, and a container another compose version created
is recreated rather than compared, because versions normalise the config differently (#237).

Every read that decides what to recreate fails closed: an unreadable lease, container list,
compose config or substrate digest refuses the apply. `--dry-run` computes the same plan against a
render staged in a temporary directory and changes nothing.

The docker CLI and the host steps sit behind two seams (`DockerState`, and the `Host` methods
`run` calls), so the orchestration is tested without docker.
"""
from __future__ import annotations

import contextlib
import dataclasses
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

from ..render import image_tags, secret_files, stack, substrate
from ..render.changed_set import (
    OPS_CONTROLLER_SERVICE,
    Change,
    DockerState,
    RenderedService,
    RunningContainer,
    Staged,
    StateUnknown,
    diff_services,
)
from . import bringup, doctor, images

OPS_CONTROLLER = OPS_CONTROLLER_SERVICE
# How long to wait for a recreated ops-controller to answer /status before the rest is recreated
# (every later bring-up reads the lease through it, and refuses while it cannot).
OPS_CONTROLLER_READY_SECONDS = 120.0
OPS_CONTROLLER_POLL_SECONDS = 3.0


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
