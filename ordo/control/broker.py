"""Process broker — drives the scheduler's decisions against real containers.

The Scheduler is the pure decision core; the Broker is the imperative shell that reconciles those
decisions into container start/stop via a pluggable backend:
  - MockBackend  — for tests (records what would start/stop).
  - DockerBackend — real, but HARD-SCOPED to its own project prefix so it can NEVER touch
    containers outside that project (a guard refuses any name outside the project).

Flow: request(job) → scheduler.submit + reconcile(); reconcile() = scheduler.pump() then start the
newly-admitted containers, stop any LRU-evicted idle residents, and RESTORE (start) any evicted
resident whose GPU-heavy work has drained. complete(job) frees the slot and reconciles (admitting
whatever was waiting; restoring the resident once the queue drains below its footprint).

This is the full media-lease contract: a lease STOPS the resident (llama.cpp) to free VRAM for a
media render and RESTARTS it automatically when the render completes — including the self-healing
path where a crashed client never completes its job (sweep_leases() force-completes it after the TTL
so the resident can never be stranded down, V1's fatal flaw).
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
from typing import Protocol

from ..render.changed_set import DockerState, StackState, Staged
from ..render.stack import compose_argv, lifecycle_group, load_compose, plan_named, profiles_in
from .scheduler import Job, Scheduler
from .scheduler_state import RECOVERY_JOB_ID, SchedulerStateStore, StateUnreadable

# Services the control plane must never cycle, because they are the ones serving the request.
#
# `agent` is the Hermes gateway. Hermes reaches these verbs through its own ops-router tools, so an
# unguarded restart is a process killing itself mid-tool-call: the request never returns, the tool
# call is recorded as failed, and the retry restarts it again. The retired ops-api encoded this by
# leaving `agent` out of a 25-name ALLOWED_SERVICES literal; a denylist states the actual rule
# instead, and does not need an edit every time a plugin is added.
#
# `ops-controller` is this process. A self-recreate orphans the compose call it is running.
#
# Everything else in the project is legitimately operator-controllable from the dashboard. When a
# render changes one of these, the post-render step (ControlPlane.apply_render) names it for the
# host's `ordo apply --only ...` instead.
SELF_REFERENTIAL_SERVICES = frozenset({"agent", "ops-controller"})


class ContainerBackend(Protocol):
    # Two kinds of argument, and the distinction is load-bearing. `service` is a COMPOSE SERVICE
    # name (`llamacpp`), resolved to a container by compose labels so the `-1` replica suffix is
    # handled. `name` is a RAW CONTAINER name (`ordo-llamacpp-1`). Mixing them up is the defect
    # that made `docker stop ordo-llamacpp` fail against a container actually called
    # `ordo-llamacpp-1`, so the parameter names say which one a method takes.
    def start(self, service: str) -> None: ...
    def stop(self, service: str) -> None: ...
    def restart(self, service: str) -> None: ...
    def logs(self, service: str, tail: int = 100) -> str: ...
    def recreate_service(self, service: str) -> None: ...
    # Recreate the named services and their netns members in ONE compose call (`stack.plan_named`,
    # `--no-deps --force-recreate`): the post-render step's changed set.
    def recreate_services(self, services: list[str]) -> None: ...
    def container_logs(self, name: str, tail: int = 100) -> str: ...
    def container_restart(self, name: str) -> None: ...

    # The read methods return the ops-api PAYLOAD, not a bare list, because the dashboard consumes
    # these shapes directly: {"services": [...]}, {"containers": [...]}. Returning a list here and
    # wrapping it at the route would put the shape in two places and let them drift.
    def list_services(self) -> dict: ...
    def list_containers(self) -> list[dict]: ...  # bare list: ops-api's shape, see DockerBackend
    def service_stats(self) -> dict: ...

    # The rendered compose this backend acts on. The control plane derives a service's netns
    # members from it (`stack.lifecycle_group`), so it reads the same file the compose verbs run.
    def rendered_compose(self) -> dict: ...

    # Both sides of the changed set (ordo/render/changed_set.py): every rendered service's config
    # hash and image, and every container of the project. Raises when either cannot be read.
    def stack_state(self) -> StackState: ...

    # Compose verbs take an OPTIONAL service: no argument means the whole project, which for
    # compose_down means the entire stack including the agent and the GPU scheduler. A named
    # service is expanded to its `lifecycle_group` (itself plus its netns members) in one call.
    def exec_in(self, container: str, command: list[str]) -> tuple[int, str]: ...
    def compose_up(self, service: str | None = None) -> None: ...
    def compose_down(self, service: str | None = None) -> None: ...
    def compose_restart(self, service: str | None = None) -> None: ...


class MockBackend:
    """Records actions instead of touching Docker — used by tests."""
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.restarted: list[str] = []
        self.log_requests: list[tuple[str, int]] = []
        self.list_services_calls: list = []
        self.recreate_calls: list[str] = []
        self.recreate_batches: list[list[str]] = []
        self.state = StackState(rendered={}, running={}, compose_version="")
        self.list_containers_calls: list = []
        self.container_log_requests: list[tuple[str, int]] = []
        self.container_restart_calls: list[str] = []
        self.service_stats_calls: list = []
        self.compose_up_calls: list = []
        self.compose_down_calls: list = []
        self.compose_restart_calls: list = []
        self.execs: list[tuple[str, list[str]]] = []
        self.exec_result: tuple[int, str] = (0, "")
        self.compose_doc: dict = {"services": {}}

    def start(self, service: str) -> None:
        self.started.append(service)

    def stop(self, service: str) -> None:
        self.stopped.append(service)

    def restart(self, service: str) -> None:
        self.restarted.append(service)

    def logs(self, service: str, tail: int = 100) -> str:
        self.log_requests.append((service, tail))
        return f"[mock logs for {service}, tail={tail}]"

    def list_services(self) -> dict:
        self.list_services_calls.append(None)
        return {"services": [
            {"id": "llamacpp", "name": "llamacpp", "state": "running", "health": "healthy",
             "status": "Up 3 hours (healthy)"},
            {"id": "dashboard", "name": "dashboard", "state": "running", "health": None,
             "status": "Up 3 hours"},
        ]}

    def recreate_service(self, service: str) -> None:
        self.recreate_calls.append(service)

    def recreate_services(self, services: list[str]) -> None:
        self.recreate_batches.append(list(services))

    def stack_state(self) -> StackState:
        return self.state

    def list_containers(self) -> list[dict]:
        self.list_containers_calls.append(None)
        return [
            {"name": "ordo-llamacpp-1", "status": "running", "image": "llamacpp:latest"},
            {"name": "ordo-dashboard-1", "status": "running", "image": "dashboard:latest"},
        ]

    def container_logs(self, name: str, tail: int = 100) -> str:
        self.container_log_requests.append((name, tail))
        return f"[mock container logs for {name}, tail={tail}]"

    def container_restart(self, name: str) -> None:
        self.container_restart_calls.append(name)

    def service_stats(self) -> dict:
        self.service_stats_calls.append(None)
        return {
            "gpu": {"total_gb": 24.0, "used_gb": 12.0, "util": 50},
            "services": {
                "llamacpp": {"cpu_pct": 10.0, "mem_gb": 2.0, "mem_pct": 5.0, "vram_gb": 8.0, "vram_pct": 33.0, "running": True},
                "dashboard": {"cpu_pct": 1.0, "mem_gb": 0.5, "mem_pct": 1.0, "vram_gb": 0.0, "vram_pct": 0.0, "running": True},
            },
            "vram_aggregate_unavailable": False,
        }

    def rendered_compose(self) -> dict:
        return self.compose_doc

    def exec_in(self, container: str, command: list[str]) -> tuple[int, str]:
        self.execs.append((container, list(command)))
        return self.exec_result

    def compose_up(self, service: str | None = None) -> None:
        self.compose_up_calls.append(service)

    def compose_down(self, service: str | None = None) -> None:
        self.compose_down_calls.append(service)

    def compose_restart(self, service: str | None = None) -> None:
        self.compose_restart_calls.append(service)


class DockerBackend:
    """Real backend, HARD-SCOPED to a compose project.

    A name passed here is a compose SERVICE name (e.g. `llamacpp`), NOT a raw container name.
    The actual container is resolved by compose LABELS
    (`com.docker.compose.project=<project>` + `com.docker.compose.service=<name>`), which is robust
    to the `-1` replica suffix compose appends (the live defect: `docker stop ordo-llamacpp`
    failed because the container is `ordo-llamacpp-1`). Resolving by label — the same mechanism
    ops-api uses — is exact and, because the project label is pinned, structurally unable to touch a
    container outside this project.

    A name that resolves to NO container in this project is a NO-OP (not an error): an abstract lease
    job (e.g. a Hermes media lease that runs its own ComfyUI workflow) has no container of its own —
    it only reserves/releases VRAM. Residents (llama.cpp) always resolve, so evict/restore act on the
    real container. The `_guard` still refuses any name that would escape the project prefix.
    """
    def __init__(self, project: str = "ordo"):
        self.project = project

    # See SELF_REFERENTIAL_SERVICES: the services serving the request, never cycled from here.
    SELF_REFERENTIAL = SELF_REFERENTIAL_SERVICES

    def _lifecycle_guard(self, service: str) -> str:
        """`_guard`, plus the refusal to act on whatever is serving this request."""
        service = self._guard(service)
        if service in self.SELF_REFERENTIAL:
            raise ValueError(
                f"{service!r} runs the control plane itself and cannot be cycled through it; "
                "use docker/compose from the host"
            )
        return service

    def _guard(self, service: str) -> str:
        """Reject anything that isn't a bare compose service name for THIS project.

        The real scoping is the compose-project LABEL filter in `_resolve` (it can only ever match a
        container whose `com.docker.compose.project` == self.project). This is belt-and-braces: refuse
        an argument shaped like a raw container path or another project's container so a caller can't
        smuggle one in. A service that already carries this project's prefix is normalized back to the
        bare service name for the label filter.
        """
        if "/" in service or service.strip() != service or not service:
            raise ValueError(f"not a valid compose service name for '{self.project}': {service!r}")
        if service.startswith(f"{self.project}-"):
            # tolerate the fully-qualified form: strip the project prefix (and any -N replica suffix)
            core = service[len(self.project) + 1:]
            return core.rsplit("-", 1)[0] if core.rsplit("-", 1)[-1].isdigit() else core
        return service

    def _resolve(self, service: str) -> str | None:  # pragma: no cover - needs real docker
        """Compose service name -> running/stopped container name, or None if the service isn't in
        this project. Uses the compose labels so the `-1` replica suffix is handled exactly."""
        service = self._guard(service)
        proc = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={self.project}",
             "--filter", f"label=com.docker.compose.service={service}", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30,
        )
        names = [n for n in proc.stdout.splitlines() if n.strip()]
        return names[0] if names else None

    def start(self, service: str) -> None:  # pragma: no cover - needs real docker
        container = self._resolve(self._lifecycle_guard(service))
        if container is None:
            return  # abstract lease job — nothing to start (caller owns its workload)
        subprocess.run(["docker", "start", container], check=True, timeout=60)

    def stop(self, service: str) -> None:  # pragma: no cover - needs real docker
        container = self._resolve(self._lifecycle_guard(service))
        if container is None:
            return  # abstract lease job — no container to stop
        subprocess.run(["docker", "stop", container], check=True, timeout=60)

    def restart(self, service: str) -> None:  # pragma: no cover - needs real docker
        container = self._resolve(self._lifecycle_guard(service))
        if container is None:
            return  # abstract lease job — no container to restart
        subprocess.run(["docker", "restart", container], check=True, timeout=60)

    def logs(self, service: str, tail: int = 100) -> str:  # pragma: no cover - needs real docker
        container = self._resolve(service)
        if container is None:
            return f"[no container found for service {service}]"
        proc = subprocess.run(
            ["docker", "logs", "--tail", str(tail), container],
            capture_output=True, text=True, timeout=30,
        )
        return proc.stdout

    # --- compose-project queries and lifecycle (ported from ops-api, slice 1) ---
    #
    # Everything below stays in this class's existing style: the docker CLI over subprocess,
    # scoped to THIS compose project by label, no third-party SDK. `_compose()` builds its argv with
    # `stack.compose_argv`, the same builder the host's `ordo up` uses, so both env files and the
    # profile set are identical whether the control plane or the operator runs compose.

    COMPOSE_DIR = "/config"

    def _compose(self, *args: str, all_profiles: bool = False) -> list[str]:
        # all_profiles: every profile in the rendered file, so a target whose `depends_on:` names
        # a PROFILED service resolves (see compose_argv).
        profiles = self._profiles() if all_profiles else []
        return compose_argv(self.COMPOSE_DIR, self.project, *args, profiles=profiles)

    def _profiles(self) -> list[str]:  # pragma: no cover - reads the rendered compose file
        """Every profile named anywhere in the rendered compose file, sorted for determinism."""
        try:
            doc = self.rendered_compose()
        except Exception:
            return []
        return profiles_in(doc)

    def rendered_compose(self) -> dict:
        """The rendered compose file. Raises OSError / yaml.YAMLError when unreadable: a caller
        that cannot see the netns members must refuse rather than orphan them."""
        return load_compose(self.COMPOSE_DIR)

    def _project_ps(self) -> list[dict]:  # pragma: no cover - needs real docker
        """Every container in this project as {service, name, state, status}."""
        proc = subprocess.run(
            ["docker", "ps", "-a",
             "--filter", f"label=com.docker.compose.project={self.project}",
             "--format", "{{.Label \"com.docker.compose.service\"}}\t{{.Names}}\t{{.State}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=30,
        )
        rows = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 4 and parts[0]:
                rows.append({"service": parts[0], "name": parts[1], "state": parts[2], "status": parts[3]})
        return rows

    @staticmethod
    def _health_from_status(status: str) -> str | None:
        """docker's Status string carries the healthcheck verdict in parentheses, or nothing at
        all when the container declares no healthcheck. None means "no healthcheck", which is not
        the same as unhealthy: the dashboard needs that distinction to avoid calling a headless
        worker down."""
        m = re.search(r"\((healthy|unhealthy|health: starting|starting)\)", status)
        if not m:
            return None
        return "starting" if "starting" in m.group(1) else m.group(1)

    @classmethod
    def _service_row(cls, r: dict) -> dict:
        """One `docker ps` row as the /services payload. `status` is docker's raw text, kept
        because the exit code ("Exited (0)" is a finished job, "Exited (1)" a crash) and the
        uptime ("Up 3 hours") exist nowhere else."""
        return {"id": r["service"], "name": r["name"], "state": r["state"],
                "health": cls._health_from_status(r["status"]), "status": r["status"]}

    def list_services(self) -> dict:  # pragma: no cover - needs real docker
        services = [self._service_row(r) for r in self._project_ps()]
        services.sort(key=lambda s: s["id"])
        return {"services": services}

    def list_containers(self) -> list[dict]:  # pragma: no cover - needs real docker
        """EVERY container on the host as a BARE LIST of {name, status, image}.

        Two things here are deliberately not what they look like they should be, and both are
        matched against the live ops-api rather than guessed:

        1. A bare list: ops-api's shape, which the dashboard is written against.
        2. NOT scoped to this compose project. ops-api returns all 86 containers on the host, where
           the project holds 53. This is the one method on this backend that deliberately looks
           outside the project, because the dashboard's container view shows the whole host. Every
           MUTATING method stays project-scoped: reading widely is safe, acting widely is not.

        Harmonising either of these is a dashboard-facing change and belongs in its own slice, not
        smuggled into a port whose whole promise is that nothing user-facing moves.
        """
        proc = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Image}}"],
            capture_output=True, text=True, timeout=30,
        )
        rows = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                rows.append({"name": parts[0], "status": parts[1], "image": parts[2]})
        return rows

    def _container_guard(self, name: str) -> str:
        """Container routes take a RAW container name, not a service name, so `_guard` does not
        apply. Refuse anything outside this project rather than trusting the caller: the whole
        point of this backend is that it structurally cannot touch another project's containers."""
        if "/" in name or name.strip() != name or not name:
            raise ValueError(f"not a valid container name: {name!r}")
        known = {r["name"] for r in self._project_ps()}
        if name not in known:
            raise ValueError(f"container {name!r} is not in project '{self.project}'")
        return name

    def container_logs(self, name: str, tail: int = 100) -> str:  # pragma: no cover - needs real docker
        container = self._container_guard(name)
        proc = subprocess.run(
            ["docker", "logs", "--tail", str(tail), container],
            capture_output=True, text=True, timeout=30,
        )
        return proc.stdout

    def container_restart(self, name: str) -> None:  # pragma: no cover - needs real docker
        subprocess.run(["docker", "restart", self._container_guard(name)], check=True, timeout=120)

    def recreate_service(self, service: str) -> None:
        """Recreate one service and its netns members. Recreate is NOT restart: an env change only
        takes effect on recreate.

        Goes through compose so the declared config is applied. A `docker run` recreate silently
        drops device reservations and compose labels (observed 2026-09-21: it produced a controller
        that reported 0GB GPU and could no longer be managed by compose).

        `--no-deps` is mandatory: without it compose cascade-recreates the target's dependencies,
        dropping their GPU pins and touching services the operator never asked about. `--force-
        recreate` so a recreate with an unchanged compose file still restarts the container and
        picks up an edited .env value. No render step: the rendered compose, with llamacpp's 5090
        uuid pin baked into its environment/deploy blocks, is replayed as it stands.

        The args come from `stack.plan_named`, the planner `ordo recreate` uses on the host: a
        netns owner (caddy) is recreated in the same call as its members, which would otherwise
        keep the destroyed namespace.
        """
        self.recreate_services([service])

    def recreate_services(self, services: list[str]) -> None:
        """`recreate_service` for several services in one compose call: the post-render step's
        changed set. Every target (members included) is refused if it is the control plane."""
        args, starts = plan_named(self.rendered_compose(), [self._lifecycle_guard(s) for s in services],
                                  force_recreate=True)
        for name in starts:
            self._lifecycle_guard(name)  # a member cannot be the control plane either
        subprocess.run(self._compose(*args, all_profiles=True), check=True, timeout=600)

    def stack_state(self) -> StackState:
        """The changed set's two sides, read the way the host's `ordo apply` reads them: this
        process's compose (pinned to the host's version, services/ops-controller/Dockerfile) hashes
        the render in /config with /config as the project directory, the directory every container
        this process recreates is created against. No service binds a project-relative path
        (tests/substrate/test_compose.py), so these hashes equal the host's, made against out/."""
        doc = self.rendered_compose()
        staged = Staged(compose_dir=self.COMPOSE_DIR, project_directory=self.COMPOSE_DIR, doc=doc)
        return DockerState().read(staged, project=self.project)

    def exec_in(self, container: str, command: list[str]) -> tuple[int, str]:  # pragma: no cover - needs real docker
        """Run a command inside one container of THIS project; returns (exit_code, combined output).

        Raises FileNotFoundError when the container is not in this project, so a caller can tell
        "not running" apart from "ran and failed".
        """
        try:
            name = self._container_guard(container)
        except ValueError as exc:
            # "not in this project" is the not-running case, which the caller reports as 503.
            raise FileNotFoundError(container) from exc
        proc = subprocess.run(
            ["docker", "exec", name, *command], capture_output=True, text=True, timeout=1800,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0 and "No such container" in output:
            raise FileNotFoundError(name)
        return proc.returncode, output

    def compose_up(self, service: str | None = None) -> None:
        # A named up is the host's `ordo up <service>`: `stack.plan_named`, so `--no-deps` (else
        # compose also starts the service's dependencies, and during a GPU lease that includes the
        # evicted llamacpp; the lease guard in api.py checks only the group) plus the netns
        # members, with every profile so a profiled dependency resolves.
        if service is None:
            subprocess.run(self._compose("up", "-d"), check=True, timeout=900)
            return
        args, _ = plan_named(self.rendered_compose(), [self._guard(service)], force_recreate=False)
        subprocess.run(self._compose(*args, all_profiles=True), check=True, timeout=900)

    def compose_restart(self, service: str | None = None) -> None:
        # Compose restarts named services in dependency order, and every member depends on its
        # owner, so the members come back after the owner has its new namespace.
        group = lifecycle_group(self.rendered_compose(), self._guard(service)) if service else []
        subprocess.run(self._compose("restart", *group), check=True, timeout=600)

    def compose_down(self, service: str | None = None) -> None:
        """Whole-project down when called with no service. This is the highest-blast-radius verb
        the backend exposes: it stops the entire stack, the agent and the GPU scheduler included.
        It is implemented because the protocol declares it, not because anything should call it
        casually. A named owner takes its netns members down with it: a member left running
        would sit in the removed namespace."""
        group = lifecycle_group(self.rendered_compose(), self._guard(service)) if service else []
        subprocess.run(self._compose("down", *group), check=True, timeout=600)

    def service_stats(self) -> dict:  # pragma: no cover - needs real docker
        """Per-service CPU and memory.

        One `docker stats --no-stream` call samples every container at once. ops-api reaches the
        same place differently: it samples containers individually, which cost ~2s each and made
        the endpoint scale at N x 2s, about 48s for this stack, so it fans them out across a
        thread pool. A single CLI call is the same idea with less machinery, and it is why this
        route is inherently slow: the daemon samples cgroup counters twice to compute a CPU delta.
        Callers must NOT paper over that with a short timeout and a zero-filled fallback.
        """
        by_name = {r["name"]: r for r in self._project_ps()}
        proc = subprocess.run(
            ["docker", "stats", "--no-stream", "--format",
             "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"],
            capture_output=True, text=True, timeout=120,
        )
        services: dict[str, dict] = {}
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 4 or parts[0] not in by_name:
                continue
            name, cpu, mem_usage, mem_pct = parts
            row = by_name[name]
            used = mem_usage.split("/")[0].strip()
            services[row["service"]] = {
                "cpu_pct": _pct(cpu),
                "mem_gb": _to_gb(used),
                "mem_pct": _pct(mem_pct),
                "vram_gb": 0.0,
                "vram_pct": 0.0,
                "running": row["state"] == "running",
            }
        for row in by_name.values():
            services.setdefault(row["service"], {
                "cpu_pct": 0.0, "mem_gb": 0.0, "mem_pct": 0.0,
                "vram_gb": 0.0, "vram_pct": 0.0, "running": row["state"] == "running",
            })
        return {"gpu": None, "services": services, "vram_aggregate_unavailable": True}



def _pct(value: str) -> float:
    """'12.34%' -> 12.34, and anything unparseable -> 0.0."""
    try:
        return float(value.strip().rstrip("%"))
    except (ValueError, AttributeError):
        return 0.0


def _to_gb(value: str) -> float:
    """docker's human sizes ('1.5GiB', '860MiB', '12kB') -> float gigabytes."""
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", (value or "").strip(), re.IGNORECASE)
    if not m:
        return 0.0
    n, unit = float(m.group(1)), m.group(2).upper().replace("I", "")
    return round(n * {"B": 1e-9, "KB": 1e-6, "MB": 1e-3, "GB": 1.0, "TB": 1000.0}.get(unit, 0.0), 3)

class Broker:
    def __init__(self, scheduler: Scheduler, backend: ContainerBackend, history=None,
                 state_store: SchedulerStateStore | None = None):
        self.scheduler = scheduler
        self.backend = backend
        # Optional LeaseHistory sink — the durable record of lease outcomes (the pure scheduler
        # keeps only live state). Wall clocks are stamped by the sink, here in the shell.
        self.history = history
        # Optional durable copy of the scheduler's live state, written on every transition so a
        # restarted ops-controller adopts the lease instead of forgetting it (restore_state).
        self.state_store = state_store
        self._state_saved = False
        # API threads and the lease loop both persist: the snapshot and the write are taken under
        # one lock so an older snapshot can never land on disk after a newer one.
        self._persist_lock = threading.Lock()

    @property
    def state_persisted(self) -> bool:
        """True when the state on disk matches the scheduler: a restart would lose nothing.

        False without a store, and after a failed write (the file is then stale). The host's
        `ordo recreate ops-controller` reads this, through /status, before recreating mid-lease.
        """
        return self.state_store is not None and self._state_saved

    def _persist(self) -> None:
        """Write the scheduler state. Called after every transition, never on a timer."""
        if self.state_store is None:
            return
        with self._persist_lock:
            try:
                self.state_store.save(self.scheduler.snapshot())
            except (OSError, RuntimeError) as e:
                # RuntimeError: another thread changed the scheduler mid-snapshot. Either way the
                # file is now behind, so stop claiming it is current; the lease loop retries.
                self._state_saved = False
                print(f"[scheduler] ERROR: cannot write the scheduler state to {self.state_store.path} "
                      f"({e}); a restart now would lose the GPU lease state", file=sys.stderr, flush=True)
                return
            self._state_saved = True

    def restore_state(self) -> None:
        """Adopt the previous process's lease state, then reconcile it. Call before serving.

        A saved lease keeps its deadline: a live holder's heartbeat renews it, and one that ran
        out while the controller was down is swept here, which restores the resident once
        nothing else needs its VRAM. An evicted resident stays evicted while any lease is live.

        An unreadable file is the dangerous case: it may have been hiding a live render. A
        resident that is running was not evicted and is left alone. A resident that is stopped
        (or whose state docker cannot report) may have been evicted for a render still on the
        card, so it stays evicted under a recovery lease that expires after one heartbeat TTL.
        Live holders learn of the loss when their heartbeat is refused and re-file, which keeps
        the resident down until they finish. The cost of a false alarm is chat on the CPU
        fallback for up to that TTL; the cost of guessing wrong the other way is a crashed host.
        """
        if self.state_store is None:
            return
        try:
            snapshot = self.state_store.load()
        except StateUnreadable as e:
            running = self._running_services()
            residents = [name for name in self.scheduler.idle_cached if running is None or name not in running]
            print(f"[scheduler] ERROR: the saved scheduler state is unreadable ({e}). Starting "
                  f"conservatively: {residents or 'no resident'} held evicted under "
                  f"'{RECOVERY_JOB_ID}' for {self.scheduler.heartbeat_ttl:.0f}s so none is restored "
                  f"beside GPU work this process cannot see", file=sys.stderr, flush=True)
            self.scheduler.hold_residents_for_recovery(residents, RECOVERY_JOB_ID, self.scheduler.heartbeat_ttl)
        else:
            if snapshot is not None:
                self.scheduler.load_snapshot(snapshot)
        self.sweep_leases()  # reconciles, and writes the adopted state back (it is not yet saved)

    def _running_services(self) -> set[str] | None:
        """The compose services running now, or None when docker cannot say."""
        try:
            listing = self.backend.list_services() or {}
        except Exception:  # noqa: BLE001 - any failure here means "unknown", handled by the caller
            return None
        return {row.get("id") for row in listing.get("services", []) if row.get("state") == "running"}

    def reconcile(self) -> bool:
        """Apply the scheduler's decisions. True when anything was admitted, evicted or restored."""
        admitted, evicted = self.scheduler.pump()
        for name in evicted:      # stop LRU-evicted idle residents first to free VRAM
            self.backend.stop(name)
        for job_id in admitted:   # then start the newly-admitted jobs
            self.backend.start(job_id)
            if self.history:
                self.history.started(job_id)
        # Finally, restore any evicted resident whose GPU-heavy work has drained (the second half of
        # the media-lease contract). take_restorable() only returns residents that fit now with an
        # empty queue, so this never thrashes the LLM between back-to-back renders.
        restored = self.scheduler.take_restorable()
        for name in restored:
            self.backend.start(name)
        return bool(admitted or evicted or restored)

    def request(self, job: Job) -> None:
        if self.history:
            self.history.submitted(job.id, job.kind, job.vram_gb)
        self.scheduler.submit(job)
        self.reconcile()
        self._persist()
        if self.history and job.id in self.scheduler.status()["rejected"]:
            self.history.rejected(job.id)

    def complete(self, job_id: str) -> None:
        if self.history:
            self.history.ended(job_id, "completed")
        self.scheduler.complete(job_id)
        self.backend.stop(job_id)
        self.reconcile()
        self._persist()

    def heartbeat(self, job_id: str) -> bool:
        """Renew a running job's lease (liveness-based). No reconcile — nothing starts or stops."""
        renewed = self.scheduler.heartbeat(job_id)
        if renewed:
            self._persist()  # the deadline moved
        return renewed

    def enforce_evictions(self) -> list[str]:
        """Stop any evicted resident that is running anyway, and return their names.

        A resident is evicted so a GPU lease can use its VRAM. Anything outside the scheduler can
        start it again (a whole-stack `docker compose up -d` starts every stopped service), which
        puts two tenants on one card: the 2026-08-08 host crash. While the scheduler holds a
        resident evicted, its view wins. Called on the serve loop's timer.
        """
        evicted = self.scheduler.evicted_residents
        if not evicted:
            return []
        running = {r.get("id") for r in (self.backend.list_services() or {}).get("services", [])
                   if r.get("state") == "running"}
        stray = sorted(set(evicted) & running)
        for name in stray:
            self.backend.stop(name)
        return stray

    def sweep_leases(self) -> list[str]:
        """Force-complete stranded leases (TTL elapsed) and reconcile — restores the resident.

        Called on a timer by `ordo serve`. Advancing the lease clock is tick()'s job (the serve loop
        ticks by the poll interval); this sweeps whatever expired and reconciles so a resident that a
        crashed client left evicted is restarted. Returns the swept job ids (for logging/audit).
        """
        expired = self.scheduler.sweep_expired_leases()
        for job_id in expired:
            self.backend.stop(job_id)  # best-effort: ensure the stranded job's container is down
            if self.history:
                self.history.ended(job_id, "swept")
        changed = self.reconcile()
        if expired or changed or not self._state_saved:
            self._persist()  # a transition, or a retry after a failed write
        return expired
