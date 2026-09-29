"""Process broker — drives the scheduler's decisions against real containers.

The Scheduler is the pure decision core; the Broker is the imperative shell that reconciles those
decisions into container start/stop via a pluggable backend:
  - MockBackend: for tests, an in-memory compose project held to DockerBackend's behaviour by
    tests/substrate/test_backend_contract.py.
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

import dataclasses
import hashlib
import itertools
import json
import re
import subprocess
import sys
import threading
from typing import Protocol

from ..render.changed_set import DockerState, RenderedService, RunningContainer, StackState, Staged
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


def summarize_inspect(raw: dict) -> dict:
    """One `docker inspect` object reduced to a FIELD ALLOWLIST: what a caller needs to reason about
    a container (image, state, health, start, restarts, mounts, networks, ports) and nothing else.

    What is left out is left out on purpose, and a test pins the exact key set:
    - the environment and the command line: they carry secret values;
    - every label: the render puts each file secret's digest in one, and an image may label anything;
    - healthcheck log output, bind specs and the rest of HostConfig: free text a service controls.
    Mount sources are paths or volume names, never file contents."""
    config = raw.get("Config") or {}
    labels = config.get("Labels") or {}
    state = raw.get("State") or {}
    health = (state.get("Health") or {}).get("Status")
    host_config = raw.get("HostConfig") or {}
    network_settings = raw.get("NetworkSettings") or {}
    mounts = []
    for mount in raw.get("Mounts") or []:
        kind = mount.get("Type")
        source = mount.get("Name") if kind == "volume" else mount.get("Source")
        mounts.append({"type": kind, "source": source, "destination": mount.get("Destination"),
                       "rw": bool(mount.get("RW"))})
    ports = []
    for container_port, bindings in sorted((network_settings.get("Ports") or {}).items()):
        host = None
        if bindings:
            first = bindings[0]
            host = f"{first.get('HostIp') or '0.0.0.0'}:{first.get('HostPort')}"
        ports.append({"container": container_port, "host": host})
    return {
        "name": str(raw.get("Name") or "").lstrip("/"),
        # Compose identity, read from its two labels and returned as plain fields: no label map.
        "project": labels.get("com.docker.compose.project", ""),
        "service": labels.get("com.docker.compose.service", ""),
        "image": config.get("Image"),
        "image_id": raw.get("Image"),
        "state": state.get("Status"),
        "health": health,
        "started_at": state.get("StartedAt"),
        "restart_count": raw.get("RestartCount"),
        "restart_policy": (host_config.get("RestartPolicy") or {}).get("Name"),
        "mounts": mounts,
        "networks": sorted((network_settings.get("Networks") or {}).keys()),
        "ports": ports,
    }


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
    # Remove the named services' STOPPED containers (`compose rm --force`, never `--stop`): the
    # post-render step's stale one-shot job containers. A running container is left alone.
    def remove_stopped_containers(self, services: list[str]) -> None: ...
    def container_logs(self, name: str, tail: int = 100) -> str: ...
    def container_restart(self, name: str) -> None: ...
    # One Ordo container, as `summarize_inspect` shapes it. ValueError when the name is not a
    # container of this project.
    def container_inspect(self, name: str) -> dict: ...

    # OTHER compose projects (ordo/control/managed.py): the control plane decides which projects
    # may be asked about; each method acts only on a container that carries that project's label,
    # and raises ValueError for any other name.
    def foreign_containers(self, project: str) -> list[dict]: ...
    def foreign_logs(self, project: str, name: str, tail: int) -> str: ...
    def foreign_inspect(self, project: str, name: str) -> dict: ...   # raw; never returned to a caller
    def foreign_restart(self, project: str, name: str) -> None: ...

    # The read methods return the ops-api PAYLOAD, not a bare list, because the dashboard consumes
    # these shapes directly: {"services": [...]}, {"containers": [...]}. Returning a list here and
    # wrapping it at the route would put the shape in two places and let them drift.
    def list_services(self) -> dict: ...
    def list_containers(self) -> list[dict]: ...  # bare list: ops-api's shape, see DockerBackend
    def service_stats(self) -> dict: ...
    # {service: docker's RestartCount} for every container of this project: how many times the
    # restart policy has restarted it (a recreate makes a new container, counting from 0). Read by
    # GET /metrics for the restart-loop alert.
    def service_restarts(self) -> dict[str, int]: ...

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
    # exec_in by compose service name: the backend finds the service's container (`-1` suffix and
    # all), so a caller never hardcodes a container name. FileNotFoundError when it has none.
    def exec_in_service(self, service: str, command: list[str]) -> tuple[int, str]: ...
    def compose_up(self, service: str | None = None) -> None: ...
    def compose_down(self, service: str | None = None) -> None: ...
    def compose_restart(self, service: str | None = None) -> None: ...


def compose_service_name(project: str, service: str) -> str:
    """Reject anything that isn't a bare compose service name for `project`; return the bare name.

    Both backends validate a SERVICE argument through here, so the fake cannot accept a name the
    real backend refuses. The real scoping is DockerBackend's compose-project LABEL filter (it can
    only ever match a container whose `com.docker.compose.project` == project). This is
    belt-and-braces: refuse an argument shaped like a raw container path or another project's
    container so a caller can't smuggle one in. A service that already carries this project's
    prefix is normalized back to the bare service name.
    """
    if "/" in service or service.strip() != service or not service:
        raise ValueError(f"not a valid compose service name for '{project}': {service!r}")
    if service.startswith(f"{project}-"):
        # tolerate the fully-qualified form: strip the project prefix (and any -N replica suffix)
        core = service[len(project) + 1:]
        return core.rsplit("-", 1)[0] if core.rsplit("-", 1)[-1].isdigit() else core
    return service


def lifecycle_service_name(project: str, service: str) -> str:
    """`compose_service_name`, plus the refusal to act on whatever is serving this request."""
    service = compose_service_name(project, service)
    if service in SELF_REFERENTIAL_SERVICES:
        raise ValueError(
            f"{service!r} runs the control plane itself and cannot be cycled through it; "
            "use docker/compose from the host"
        )
    return service


def container_name_argument(name: str) -> str:
    """A RAW container name argument, checked for shape before the project-membership check."""
    if "/" in name or name.strip() != name or not name:
        raise ValueError(f"not a valid container name: {name!r}")
    return name


@dataclasses.dataclass
class _FakeContainer:
    """One container of MockBackend's project: what `docker ps` and `docker inspect` report."""
    service: str
    name: str
    container_id: str
    image: str
    config_hash: str
    one_shot: bool                # `restart: "no"`: runs to completion when started
    has_healthcheck: bool
    owner: str | None             # the owner service of a `network_mode: service:<owner>` member
    owner_container_id: str       # the owner container a member was created against ("" otherwise)
    state: str = "created"        # created, running, exited
    exit_code: int = 0
    restart_count: int = 0        # restart-policy restarts (`docker restart` does not count)
    netns: int = 0                # an owner's network namespace: a new one on every start
    joined_netns: int = 0         # a member's: the owner namespace it joined when it last started
    output: list[str] = dataclasses.field(default_factory=list)


class MockBackend:
    """An in-memory compose project that behaves the way DockerBackend does against real docker.

    A FAKE, not a recorder. The recorder it replaced returned canned payloads whatever had
    happened, so it agreed with every caller, and twice a route that could not work against real
    docker shipped with a green suite (0867df4: methods DockerBackend lacked; 62492fd: netns members
    orphaned by owner verbs). tests/substrate/test_backend_contract.py runs one set of behavioural
    tests against this class AND against DockerBackend on a throwaway compose project, so what this
    fake does is what the real backend is proven to do.

    Two things live side by side:
      - the CALL LOG (`started`, `stopped`, `restarted`, `recreate_calls`, ...): one entry per call,
        recorded before the call is validated and whether or not it changed anything;
      - the MODELLED PROJECT (`project_containers`, by service): every read reports it and every
        verb changes it the way docker does, including the network namespace of a
        `network_mode: service:<owner>` member (attached only while it holds its owner's current
        namespace).

    The constructor brings the project up: every service `compose_doc` defines gets a started
    container, in dependency order (`ordo up --all`). Knobs a test may set:
      - `exec_result`: what a command run in a RUNNING container returns (the fake runs nothing);
      - `state`: a scripted StackState in place of the modelled one;
      - `inspect_result`: a scripted container_inspect summary in place of the modelled one.
    """

    COMPOSE_VERSION = "fake"

    def __init__(self, compose_doc: dict | None = None, project: str = "ordo") -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.restarted: list[str] = []
        self.log_requests: list[tuple[str, int]] = []
        self.list_services_calls: list = []
        self.recreate_calls: list[str] = []
        self.recreate_batches: list[list[str]] = []
        self.removed_containers: list[list[str]] = []
        self.list_containers_calls: list = []
        self.container_log_requests: list[tuple[str, int]] = []
        self.container_restart_calls: list[str] = []
        self.inspect_requests: list[str] = []
        self.inspect_result: dict | None = None
        # project -> {container name: (row, raw inspect)}; see ordo/control/managed.py
        self.foreign: dict[str, dict[str, tuple[dict, dict]]] = {}
        self.foreign_log_requests: list[tuple[str, str, int]] = []
        self.foreign_restarts: list[tuple[str, str]] = []
        self.service_stats_calls: list = []
        self.compose_up_calls: list = []
        self.compose_down_calls: list = []
        self.compose_restart_calls: list = []
        self.execs: list[tuple[str, list[str]]] = []
        self.exec_result: tuple[int, str] = (0, "")
        self.state: StackState | None = None
        self.project = project
        self.compose_doc: dict = compose_doc if compose_doc is not None else {"services": {}}
        self.project_containers: dict[str, _FakeContainer] = {}
        self._container_ids = itertools.count(1)
        self._namespaces = itertools.count(1)
        for service in self._in_dependency_order(sorted(self._services())):
            self._replace(service)

    # --- the model ---

    def _services(self) -> dict:
        return self.compose_doc.get("services") or {}

    def _owner_of(self, service: str) -> str | None:
        mode = str((self._services().get(service) or {}).get("network_mode") or "")
        return mode[len("service:"):] if mode.startswith("service:") else None

    def _unprofiled(self, services: list[str]) -> list[str]:
        """What a compose call with no `--profile` sees: a service with a profile is not there."""
        return [s for s in services if not (self._services().get(s) or {}).get("profiles")]

    def _in_dependency_order(self, services: list[str]) -> list[str]:
        """Owners before their members: compose orders a member after the owner it depends on."""
        return ([s for s in services if self._owner_of(s) is None]
                + [s for s in services if self._owner_of(s) is not None])

    def _config_hash(self, service: str) -> str:
        """The hash compose labels a container with. A member's `service:<owner>` is resolved to
        the owner's current container first, as compose does (changed_set.shared_namespace_overrides)."""
        spec = dict(self._services().get(service) or {})
        owner = self._owner_of(service)
        if owner is not None and owner in self.project_containers:
            spec["network_mode"] = f"container:{self.project_containers[owner].container_id}"
        return hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()

    def _require_defined(self, services: list[str], verb: str) -> None:
        """compose refuses a verb that names a service the file does not define."""
        for service in services:
            if service not in self._services():
                raise subprocess.CalledProcessError(1, ["docker", "compose", verb, service],
                                                    stderr=f"no such service: {service}")

    def _replace(self, service: str) -> None:
        """Remove the service's container (if any), then create and start a new one."""
        self.project_containers.pop(service, None)
        spec = self._services().get(service) or {}
        owner = self._owner_of(service)
        owner_container = self.project_containers.get(owner) if owner else None
        healthcheck = spec.get("healthcheck") or {}
        container = _FakeContainer(
            service=service, name=f"{self.project}-{service}-1",
            container_id=f"fake{next(self._container_ids):060d}", image=str(spec.get("image") or ""),
            config_hash=self._config_hash(service), one_shot=str(spec.get("restart")) == "no",
            has_healthcheck=bool(healthcheck) and not healthcheck.get("disable"),
            owner=owner, owner_container_id=owner_container.container_id if owner_container else "")
        self.project_containers[service] = container
        self._boot(container)

    def _boot(self, container: _FakeContainer) -> None:
        """`docker start`. A member joins its owner's CURRENT namespace, which needs the owner
        container it was created against to exist and run; anything else gets a new namespace."""
        if container.owner is not None:
            owner = next((c for c in self.project_containers.values()
                          if c.container_id == container.owner_container_id), None)
            if owner is None:
                raise subprocess.CalledProcessError(
                    1, ["docker", "start", container.name],
                    stderr=f"joining network namespace of container: No such container: "
                           f"{container.owner_container_id}")
            if owner.state != "running":
                raise subprocess.CalledProcessError(
                    1, ["docker", "start", container.name],
                    stderr=f"cannot join network namespace of a non running container: {owner.name}")
            container.joined_netns = owner.netns
        else:
            container.netns = next(self._namespaces)
        container.state, container.exit_code = ("exited", 0) if container.one_shot else ("running", 0)

    @staticmethod
    def _halt(container: _FakeContainer) -> None:
        """`docker stop`: SIGTERM, which the stack's containers exit on."""
        if container.state == "running":
            container.state, container.exit_code = "exited", 143

    def _cycle(self, container: _FakeContainer) -> None:
        """`docker restart`."""
        self._halt(container)
        self._boot(container)

    def _named_container(self, name: str) -> _FakeContainer:
        name = container_name_argument(name)
        for container in self.project_containers.values():
            if container.name == name:
                return container
        raise ValueError(f"container {name!r} is not in project '{self.project}'")

    @staticmethod
    def _tail(container: _FakeContainer, tail: int) -> str:
        lines = container.output[-tail:] if tail > 0 else []
        return "".join(f"{line}\n" for line in lines)

    def network_attached(self, service: str) -> bool:
        """Whether the service's container has a network beyond `lo`: it runs and, for a netns
        member, still holds the namespace its owner has now."""
        container = self.project_containers.get(service)
        if container is None or container.state != "running":
            return False
        if container.owner is None:
            return True
        owner = self.project_containers.get(container.owner)
        return (owner is not None and owner.container_id == container.owner_container_id
                and owner.state == "running" and owner.netns == container.joined_netns)

    # --- ContainerBackend ---

    def start(self, service: str) -> None:
        self.started.append(service)
        container = self.project_containers.get(lifecycle_service_name(self.project, service))
        if container is not None and container.state != "running":
            self._boot(container)

    def stop(self, service: str) -> None:
        self.stopped.append(service)
        container = self.project_containers.get(lifecycle_service_name(self.project, service))
        if container is not None:
            self._halt(container)

    def restart(self, service: str) -> None:
        self.restarted.append(service)
        container = self.project_containers.get(lifecycle_service_name(self.project, service))
        if container is not None:
            self._cycle(container)

    def logs(self, service: str, tail: int = 100) -> str:
        self.log_requests.append((service, tail))
        container = self.project_containers.get(compose_service_name(self.project, service))
        if container is None:
            return f"[no container found for service {service}]"
        return self._tail(container, tail)

    def list_services(self) -> dict:
        self.list_services_calls.append(None)
        rows = [DockerBackend._service_row({"service": c.service, "name": c.name, "state": c.state,
                                            "status": self._status(c)})
                for c in self.project_containers.values()]
        rows.sort(key=lambda row: row["id"])
        return {"services": rows}

    def recreate_service(self, service: str) -> None:
        self.recreate_calls.append(service)
        self._recreate([service])

    def recreate_services(self, services: list[str]) -> None:
        self.recreate_batches.append(list(services))
        self._recreate(services)

    def _recreate(self, services: list[str]) -> None:
        """`up -d --no-deps --force-recreate` of the named services and their netns members."""
        _args, targets = plan_named(self.rendered_compose(),
                                    [lifecycle_service_name(self.project, s) for s in services],
                                    force_recreate=True)
        for name in targets:
            lifecycle_service_name(self.project, name)  # a member cannot be the control plane either
        self._require_defined(sorted(targets), "up")
        for name in self._in_dependency_order(sorted(targets)):
            self._replace(name)

    def remove_stopped_containers(self, services: list[str]) -> None:
        self.removed_containers.append(list(services))
        names = [lifecycle_service_name(self.project, s) for s in services]
        self._require_defined(names, "rm")
        for name in names:
            container = self.project_containers.get(name)
            if container is not None and container.state != "running":
                del self.project_containers[name]

    def stack_state(self) -> StackState:
        if self.state is not None:
            return self.state
        rendered = {
            name: RenderedService(service=name, config_hash=self._config_hash(name),
                                  image_ref=str((spec or {}).get("image") or ""),
                                  image_id=f"fake-image:{(spec or {}).get('image')}",
                                  one_shot=str((spec or {}).get("restart")) == "no")
            for name, spec in self._services().items()
        }
        running = {
            name: RunningContainer(service=name, config_hash=c.config_hash, image_id=f"fake-image:{c.image}",
                                   compose_version=self.COMPOSE_VERSION, container_id=c.container_id,
                                   state=c.state)
            for name, c in self.project_containers.items()
        }
        return StackState(rendered=rendered, running=running, compose_version=self.COMPOSE_VERSION)

    def _status(self, container: _FakeContainer) -> str:
        """docker's Status text for the container."""
        if container.state == "running":
            return "Up 1 second" + (" (healthy)" if container.has_healthcheck else "")
        if container.state == "exited":
            return f"Exited ({container.exit_code}) 1 second ago"
        return "Created"

    def list_containers(self) -> list[dict]:
        self.list_containers_calls.append(None)
        # `docker ps` shows a digest-pinned image by its tag alone.
        return [{"name": c.name, "status": c.state, "image": c.image.split("@", 1)[0], "project": self.project,
                 "service": c.service, "health": DockerBackend._health_from_status(self._status(c))}
                for c in self.project_containers.values()]

    def container_logs(self, name: str, tail: int = 100) -> str:
        self.container_log_requests.append((name, tail))
        return self._tail(self._named_container(name), tail)

    def container_restart(self, name: str) -> None:
        self.container_restart_calls.append(name)
        self._cycle(self._named_container(name))

    def container_inspect(self, name: str) -> dict:
        self.inspect_requests.append(name)
        container = self._named_container(name)
        if self.inspect_result is not None:
            return self.inspect_result
        spec = self._services().get(container.service) or {}
        return {
            "name": container.name, "project": self.project, "service": container.service,
            "image": container.image, "image_id": f"fake-image:{container.image}", "state": container.state,
            "health": "healthy" if container.has_healthcheck and container.state == "running" else None,
            "started_at": "", "restart_count": 0, "restart_policy": str(spec.get("restart") or "no"),
            "mounts": [],
            # A netns member has no network of its own; everything else is on the project default.
            "networks": [] if container.owner else [f"{self.project}_default"],
            "ports": [],
        }

    def _foreign(self, project: str, name: str) -> tuple[dict, dict]:
        entry = self.foreign.get(project, {}).get(name)
        if entry is None:
            raise ValueError(f"container {name!r} is not in project {project!r}")
        return entry

    def foreign_containers(self, project: str) -> list[dict]:
        return [row for row, _raw in self.foreign.get(project, {}).values()]

    def foreign_logs(self, project: str, name: str, tail: int) -> str:
        self._foreign(project, name)
        self.foreign_log_requests.append((project, name, tail))
        return f"[mock logs for {project}/{name}, tail={tail}]"

    def foreign_inspect(self, project: str, name: str) -> dict:
        return self._foreign(project, name)[1]

    def foreign_restart(self, project: str, name: str) -> None:
        self._foreign(project, name)
        self.foreign_restarts.append((project, name))

    def service_restarts(self) -> dict[str, int]:
        return {c.service: c.restart_count for c in self.project_containers.values()}

    def service_stats(self) -> dict:
        self.service_stats_calls.append(None)
        idle = {"cpu_pct": 0.0, "mem_gb": 0.0, "mem_pct": 0.0, "vram_gb": 0.0, "vram_pct": 0.0}
        return {
            "gpu": None,
            "services": {c.service: {**idle, "running": c.state == "running"}
                         for c in self.project_containers.values()},
            "vram_aggregate_unavailable": True,
        }

    def rendered_compose(self) -> dict:
        return self.compose_doc

    def exec_in(self, container: str, command: list[str]) -> tuple[int, str]:
        self.execs.append((container, list(command)))
        try:
            target = self._named_container(container)
        except ValueError as exc:
            raise FileNotFoundError(container) from exc
        if target.state != "running":
            return 1, f"Error response from daemon: container {target.container_id} is not running\n"
        return self.exec_result

    def exec_in_service(self, service: str, command: list[str]) -> tuple[int, str]:
        container = self.project_containers.get(compose_service_name(self.project, service))
        if container is None:
            raise FileNotFoundError(service)
        return self.exec_in(container.name, command)

    def compose_up(self, service: str | None = None) -> None:
        self.compose_up_calls.append(service)
        if service is None:
            targets = self._unprofiled(sorted(self._services()))
        else:
            _args, named = plan_named(self.rendered_compose(), [compose_service_name(self.project, service)],
                                      force_recreate=False)
            targets = sorted(named)
            self._require_defined(targets, "up")
        for name in self._in_dependency_order(targets):
            container = self.project_containers.get(name)
            if container is None or container.config_hash != self._config_hash(name):
                self._replace(name)
            elif container.state != "running":
                self._boot(container)

    def compose_down(self, service: str | None = None) -> None:
        self.compose_down_calls.append(service)
        if service is None:
            # A bare `compose down` runs with no profile active, so it leaves profiled services up.
            for name in self._unprofiled(sorted(self.project_containers)):
                del self.project_containers[name]
            return
        group = lifecycle_group(self.rendered_compose(), compose_service_name(self.project, service))
        self._require_defined(group, "down")
        for name in group:
            self.project_containers.pop(name, None)

    def compose_restart(self, service: str | None = None) -> None:
        self.compose_restart_calls.append(service)
        if service is None:
            group = self._unprofiled(sorted(self.project_containers))   # as down: no profile active
        else:
            group = lifecycle_group(self.rendered_compose(), compose_service_name(self.project, service))
            self._require_defined(group, "restart")
        for name in self._in_dependency_order(group):
            if name in self.project_containers:
                self._cycle(self.project_containers[name])


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
        return lifecycle_service_name(self.project, service)

    def _guard(self, service: str) -> str:
        """Reject anything that isn't a bare compose service name for THIS project
        (`compose_service_name`, shared with MockBackend)."""
        return compose_service_name(self.project, service)

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

    def _project_ps(self, project: str | None = None) -> list[dict]:  # pragma: no cover - needs real docker
        """Every container of a compose project (this one by default) as {service, name, state,
        status, image}."""
        proc = subprocess.run(
            ["docker", "ps", "-a", "--no-trunc",
             "--filter", f"label=com.docker.compose.project={project or self.project}",
             "--format",
             "{{.Label \"com.docker.compose.service\"}}\t{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}\t{{.ID}}"],
            capture_output=True, text=True, timeout=30,
        )
        rows = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 6 and parts[0]:
                rows.append({"service": parts[0], "name": parts[1], "state": parts[2], "status": parts[3],
                             "image": parts[4], "id": parts[5]})
        return rows

    # --- OTHER compose projects (ordo/control/managed.py) ---
    # The control plane only asks about projects the operator listed. These methods add the
    # structural half: each acts only on a container that carries that project's compose label.

    def _foreign_guard(self, project: str, name: str) -> str:
        """The full container ID of the container NAMED `name` in `project`, from the
        label-filtered `docker ps`. Every foreign verb acts on that ID, never on the name: docker
        resolves a name-or-ID argument by ID first, so a container named after another
        container's ID would otherwise redirect the verb to that other container."""
        if project == self.project:
            raise ValueError(f"'{project}' is this stack's own project; its own verbs maintain it")
        if "/" in name or name.strip() != name or not name:
            raise ValueError(f"not a valid container name: {name!r}")
        ids = [r["id"] for r in self._project_ps(project) if r["name"] == name and r.get("id")]
        if len(ids) != 1:
            raise ValueError(f"container {name!r} is not in project {project!r}")
        return ids[0]

    def foreign_containers(self, project: str) -> list[dict]:  # pragma: no cover - needs real docker
        if project == self.project:
            raise ValueError(f"'{project}' is this stack's own project; its own verbs maintain it")
        return [{"name": r["name"], "service": r["service"], "state": r["state"],
                 "health": self._health_from_status(r["status"]), "status": r["status"], "image": r["image"]}
                for r in self._project_ps(project)]

    def foreign_logs(self, project: str, name: str, tail: int) -> str:  # pragma: no cover - needs real docker
        proc = subprocess.run(["docker", "logs", "--tail", str(tail), self._foreign_guard(project, name)],
                              capture_output=True, text=True, timeout=30)
        return proc.stdout + proc.stderr

    def foreign_inspect(self, project: str, name: str) -> dict:  # pragma: no cover - needs real docker
        proc = subprocess.run(["docker", "inspect", "--type", "container", self._foreign_guard(project, name)],
                              capture_output=True, text=True, timeout=30, check=True)
        return json.loads(proc.stdout)[0]

    def foreign_restart(self, project: str, name: str) -> None:  # pragma: no cover - needs real docker
        subprocess.run(["docker", "restart", self._foreign_guard(project, name)], check=True, timeout=120)

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
            ["docker", "ps", "-a", "--format", self.CONTAINER_PS_FORMAT],
            capture_output=True, text=True, timeout=30,
        )
        return [row for line in proc.stdout.splitlines() if (row := self.container_row(line))]

    # `docker ps` columns for list_containers, in the order container_row reads them.
    CONTAINER_PS_FORMAT = ("{{.Names}}\t{{.State}}\t{{.Image}}\t{{.Label \"com.docker.compose.project\"}}"
                           "\t{{.Label \"com.docker.compose.service\"}}\t{{.Status}}")

    @classmethod
    def container_row(cls, line: str) -> dict | None:
        """One CONTAINER_PS_FORMAT line as a /containers row, or None for a malformed line.
        `status` keeps its ops-api meaning (docker's State); project and service are empty for a
        container compose did not create; health is None without a healthcheck."""
        parts = line.split("\t")
        if len(parts) != 6:
            return None
        name, state, image, project, service, status = parts
        return {"name": name, "status": state, "image": image, "project": project, "service": service,
                "health": cls._health_from_status(status)}

    def _container_guard(self, name: str) -> str:
        """Container routes take a RAW container name, not a service name, so `_guard` does not
        apply. Refuse anything outside this project rather than trusting the caller: the whole
        point of this backend is that it structurally cannot touch another project's containers."""
        name = container_name_argument(name)
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

    def container_inspect(self, name: str) -> dict:  # pragma: no cover - needs real docker
        container = self._container_guard(name)
        proc = subprocess.run(["docker", "inspect", "--type", "container", container],
                              capture_output=True, text=True, timeout=30, check=True)
        return summarize_inspect(json.loads(proc.stdout)[0])

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

    def remove_stopped_containers(self, services: list[str]) -> None:
        """`docker compose rm --force` for the named services. Without `--stop`, compose removes
        only stopped containers, so a job started since the changed set was read keeps running.
        Every profile, so a profiled job (evals) resolves."""
        names = [self._lifecycle_guard(s) for s in services]
        subprocess.run(self._compose("rm", "--force", *names, all_profiles=True), check=True, timeout=120)

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

    def exec_in_service(self, service: str, command: list[str]) -> tuple[int, str]:  # pragma: no cover - needs real docker
        container = self._resolve(service)
        if container is None:
            raise FileNotFoundError(service)
        return self.exec_in(container, command)

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
        group = lifecycle_group(self.rendered_compose(), self._guard(service)) if service is not None else []
        subprocess.run(self._compose("restart", *group), check=True, timeout=600)

    def compose_down(self, service: str | None = None) -> None:
        """Whole-project down when called with no service. This is the highest-blast-radius verb
        the backend exposes: it stops the entire stack, the agent and the GPU scheduler included.
        It is implemented because the protocol declares it, not because anything should call it
        casually. A named owner takes its netns members down with it: a member left running
        would sit in the removed namespace. Only None means the whole project: an empty name is
        malformed (`_guard` refuses it), never a whole-stack down."""
        group = lifecycle_group(self.rendered_compose(), self._guard(service)) if service is not None else []
        subprocess.run(self._compose("down", *group), check=True, timeout=600)

    def service_restarts(self) -> dict[str, int]:  # pragma: no cover - needs real docker
        """docker's RestartCount per service of this project: one `docker ps` for the container IDs
        (label-scoped, like every read here) and one `docker inspect` of all of them."""
        ids = [row["id"] for row in self._project_ps() if row.get("id")]
        if not ids:
            return {}
        proc = subprocess.run(
            ["docker", "inspect", "--type", "container", "--format",
             "{{index .Config.Labels \"com.docker.compose.service\"}}\t{{.RestartCount}}", *ids],
            capture_output=True, text=True, timeout=30, check=True,
        )
        counts: dict[str, int] = {}
        for line in proc.stdout.splitlines():
            service, _, count = line.partition("\t")
            if service and count.strip().isdigit():
                counts[service] = int(count)
        return counts

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
        # One container-changing operation at a time: a lease transition here (decide, then stop and
        # start containers) or a lifecycle verb in ControlPlane, which takes this same lock. Without
        # it a lease admitted in the middle of a recreate could stop llama.cpp while compose brings
        # it back, putting two tenants on one card (the 2026-08-08 crash), and two reconciles could
        # issue their stops and starts out of order. It is held across docker calls (a recreate can
        # hold it for 15 minutes), so no HTTP call ever waits for it: lease calls try it and leave
        # the reconcile to the lease sweep when it is taken (`_reconcile_unless_busy`), and
        # heartbeats, /status and /health never touch it. Reentrant because restore_state sweeps
        # while holding it.
        self.operation_lock = threading.RLock()

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
            except OSError as e:
                # The file is now behind, so stop claiming it is current; the lease loop retries.
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
        with self.operation_lock:
            self._restore_state()

    def _restore_state(self) -> None:
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
        """Apply the scheduler's decisions. True when anything was admitted, evicted or restored.
        Callers hold the operation lock."""
        rejected_before = set(self.scheduler.status()["rejected"])
        admitted, evicted = self.scheduler.pump()
        if self.history:
            for job_id in set(self.scheduler.status()["rejected"]) - rejected_before:
                self.history.rejected(job_id)
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

    def _reconcile_unless_busy(self) -> None:
        """Reconcile now, or, while another operation holds the operation lock, leave it to the
        lease sweep's next tick (sweep_leases reconciles every tick). Lease calls never wait for the
        lock: their clients time out after 30 s, and a call still queued behind a 15-minute recreate
        after its client gave up would admit a lease nobody holds. A request left queued is safe:
        clients poll /status until admitted, and one that gives up withdraws it (complete)."""
        if not self.operation_lock.acquire(blocking=False):
            return
        try:
            self.reconcile()
        finally:
            self.operation_lock.release()

    def request(self, job: Job) -> None:
        if self.history:
            self.history.submitted(job.id, job.kind, job.vram_gb)
        self.scheduler.submit(job)
        self._reconcile_unless_busy()
        self._persist()

    def complete(self, job_id: str) -> None:
        if self.history:
            self.history.ended(job_id, "completed")
        self.scheduler.complete(job_id)  # takes effect at once: a withdrawn request is never admitted
        self.backend.stop(job_id)  # the job's own container, when it has one
        self._reconcile_unless_busy()
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

        It takes no operation lock, so it keeps guarding the card while a long verb runs. The
        evicted set is read again AFTER the container listing: a restore removes the resident from
        that set before starting it, so a resident that is running because it was just restored is
        never mistaken for a stray and stopped (which would strand it down).
        """
        if not self.scheduler.evicted_residents:
            return []
        running = {r.get("id") for r in (self.backend.list_services() or {}).get("services", [])
                   if r.get("state") == "running"}
        stray = sorted(set(self.scheduler.evicted_residents) & running)
        for name in stray:
            self.backend.stop(name)
        return stray

    def sweep_leases(self) -> list[str]:
        """Force-complete stranded leases (TTL elapsed) and reconcile — restores the resident.

        Called on a timer by `ordo serve`. Advancing the lease clock is tick()'s job (the serve loop
        ticks by the poll interval); this sweeps whatever expired and reconciles so a resident that a
        crashed client left evicted is restarted. Returns the swept job ids (for logging/audit).

        While another operation holds the operation lock (a recreate can take minutes) the sweep
        does nothing and returns []: the timer calls it again on the next tick, and an expired lease
        stays expired until then.
        """
        if not self.operation_lock.acquire(blocking=False):
            return []
        try:
            expired = self.scheduler.sweep_expired_leases()
            for job_id in expired:
                self.backend.stop(job_id)  # best-effort: ensure the stranded job's container is down
                if self.history:
                    self.history.ended(job_id, "swept")
            changed = self.reconcile()
            if expired or changed or not self._state_saved:
                self._persist()  # a transition, or a retry after a failed write
            return expired
        finally:
            self.operation_lock.release()
