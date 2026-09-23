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
from typing import Protocol

from .scheduler import Job, Scheduler


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
    def container_logs(self, name: str, tail: int = 100) -> str: ...
    def container_restart(self, name: str) -> None: ...

    # The read methods return the ops-api PAYLOAD, not a bare list, because the dashboard consumes
    # these shapes directly: {"services": [...]}, {"containers": [...]}. Returning a list here and
    # wrapping it at the route would put the shape in two places and let them drift.
    def list_services(self) -> dict: ...
    def list_containers(self) -> list[dict]: ...  # bare list: ops-api's shape, see DockerBackend
    def mcp_containers(self) -> dict: ...
    def service_stats(self) -> dict: ...

    # Compose verbs take an OPTIONAL service: no argument means the whole project, which for
    # compose_down means the entire stack including the agent and the GPU scheduler.
    def pull_image(self, service: str) -> None: ...
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
        self.list_containers_calls: list = []
        self.container_log_requests: list[tuple[str, int]] = []
        self.container_restart_calls: list[str] = []
        self.service_stats_calls: list = []
        self.mcp_containers_calls: list = []
        self.compose_up_calls: list = []
        self.compose_down_calls: list = []
        self.compose_restart_calls: list = []
        self.pulled: list[str] = []
        self.execs: list[tuple[str, list[str]]] = []
        self.exec_result: tuple[int, str] = (0, "")

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
            {"id": "llamacpp", "name": "llamacpp", "state": "running", "health": "healthy"},
            {"id": "dashboard", "name": "dashboard", "state": "running", "health": None},
        ]}

    def recreate_service(self, service: str) -> None:
        self.recreate_calls.append(service)

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

    def mcp_containers(self) -> dict:
        self.mcp_containers_calls.append(None)
        return {"containers": [
            {"id": "gateway", "name": "ordo-mcp-gateway-1", "service": "mcp-gateway",
             "status": "running", "image": "mcp-gateway:latest"},
        ]}

    def pull_image(self, service: str) -> None:
        self.pulled.append(service)

    def exec_in(self, container: str, command: list[str]) -> tuple[int, str]:
        self.execs.append((container, list(command)))
        return self.exec_result

    def compose_up(self, service: str | None = None) -> None:
        self.compose_up_calls.append(None)

    def compose_down(self, service: str | None = None) -> None:
        self.compose_down_calls.append(None)

    def compose_restart(self, service: str | None = None) -> None:
        self.compose_restart_calls.append(None)


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

    # Services the control plane must never cycle, because they are the ones serving the request.
    #
    # `agent` is the Hermes gateway. Hermes reaches these verbs through its own ops-router tools,
    # so an unguarded restart is a process killing itself mid-tool-call: the request never returns,
    # the tool call is recorded as failed, and the retry restarts it again. The retired ops-api
    # encoded this by leaving `agent` out of a 25-name ALLOWED_SERVICES literal; a denylist states
    # the actual rule instead, and does not need an edit every time a plugin is added.
    #
    # `ops-controller` is this process. A self-recreate orphans the compose call it is running.
    #
    # Everything else in the project is legitimately operator-controllable from the dashboard.
    SELF_REFERENTIAL = frozenset({"agent", "ops-controller"})

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
    # scoped to THIS compose project by label, no third-party SDK. `_compose()` builds the
    # invocation the operator uses by hand, including BOTH env files, because a compose call
    # missing secrets.env renders a different file than the one the stack was brought up with.

    COMPOSE_DIR = "/config"

    def _compose(self, *args: str, all_profiles: bool = False) -> list[str]:
        cmd = [
            "docker", "compose", "-p", self.project,
            "-f", f"{self.COMPOSE_DIR}/docker-compose.yml",
        ]
        # Every profile the stack was started with, so a target whose `depends_on:` names a
        # PROFILED service resolves. Without it, `docker compose ... open-webui` aborts with
        # "no such service: qdrant" (qdrant sits behind the `rag` profile) even though
        # --no-deps means qdrant is never started. Widening the resolvable set is safe;
        # --no-deps is what guarantees only the named service is touched.
        if all_profiles:
            for profile in self._profiles():
                cmd += ["--profile", profile]
        # BOTH env files. Passing any --env-file disables compose's implicit .env auto-load, so
        # .env must be listed too; without secrets.env every ${LITELLM_MASTER_KEY} style
        # reference goes UNSET and secret-dependent services crash-loop (the 2026-06-26
        # oauth2-proxy 11-byte-cookie outage). Order matters: derived first, secrets second.
        cmd += [
            "--env-file", f"{self.COMPOSE_DIR}/.env",
            "--env-file", f"{self.COMPOSE_DIR}/secrets.env",
        ]
        return cmd + list(args)

    def _profiles(self) -> list[str]:  # pragma: no cover - reads the rendered compose file
        """Every profile named anywhere in the rendered compose file, sorted for determinism."""
        try:
            import yaml

            with open(f"{self.COMPOSE_DIR}/docker-compose.yml", encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
        except Exception:
            return []
        found: set[str] = set()
        for service in (doc.get("services") or {}).values():
            for profile in (service or {}).get("profiles") or []:
                found.add(str(profile))
        return sorted(found)

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

    def list_services(self) -> dict:  # pragma: no cover - needs real docker
        services = [
            {"id": r["service"], "name": r["name"], "state": r["state"],
             "health": self._health_from_status(r["status"])}
            for r in self._project_ps()
        ]
        services.sort(key=lambda s: s["id"])
        return {"services": services}

    def list_containers(self) -> list[dict]:  # pragma: no cover - needs real docker
        """EVERY container on the host as a BARE LIST of {name, status, image}.

        Two things here are deliberately not what they look like they should be, and both are
        matched against the live ops-api rather than guessed:

        1. A bare list, while its sibling mcp_containers returns {"containers": [...]}. ops-api is
           inconsistent between those two routes and the dashboard is written against both as they
           are.
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

    def mcp_containers(self) -> dict:  # pragma: no cover - needs real docker
        """Containers labelled ordo.mcp=true, scoped to this project.

        Fields match ops-api exactly (id, name, service, status, image) because the dashboard's MCP
        cards read them by name. Note that ops-api is not internally consistent between its two
        container routes: this one returns {"containers": [...]} while /containers returns a bare
        list. Both shapes are reproduced as they are; harmonising them is a dashboard-facing change
        and belongs in its own slice rather than hidden inside a port.
        """
        proc = subprocess.run(
            ["docker", "ps", "-a",
             "--filter", f"label=com.docker.compose.project={self.project}",
             "--filter", "label=ordo.mcp=true",
             "--format", '{{.Names}}\t{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Image}}'],
            capture_output=True, text=True, timeout=30,
        )
        rows = []
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 4:
                service = parts[1]
                rows.append({
                    # `id` is the service name minus the mcp- prefix, which is what the dashboard
                    # keys its MCP cards on; ops-api derives it the same way.
                    "id": service[len("mcp-"):] if service.startswith("mcp-") else service,
                    "name": parts[0], "service": service,
                    "status": parts[2], "image": parts[3],
                })
        return {"containers": rows}

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

    def recreate_service(self, service: str) -> None:  # pragma: no cover - needs real docker
        """Recreate exactly one service. Recreate is NOT restart: an env change only takes effect
        on recreate.

        Goes through compose so the declared config is applied. A `docker run` recreate silently
        drops device reservations and compose labels (observed 2026-09-21: it produced a controller
        that reported 0GB GPU and could no longer be managed by compose).

        `--no-deps` is mandatory: without it compose cascade-recreates the target's dependencies,
        dropping their GPU pins and touching services the operator never asked about. `--force-
        recreate` so a recreate with an unchanged compose file still restarts the container and
        picks up an edited .env value. No render step: the rendered compose, with llamacpp's 5090
        uuid pin baked into its environment/deploy blocks, is replayed as it stands.
        """
        subprocess.run(
            self._compose("up", "-d", "--no-deps", "--force-recreate",
                          self._lifecycle_guard(service), all_profiles=True),
            check=True, timeout=600,
        )

    def pull_image(self, service: str) -> None:  # pragma: no cover - needs real docker
        """Pull this service's declared image. Compose, not `docker pull`, because the image
        reference lives in the rendered compose file and nowhere else."""
        service = self._guard(service)
        proc = subprocess.run(
            self._compose("pull", service), capture_output=True, text=True, timeout=1800,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip()[:500])

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

    def compose_up(self, service: str | None = None) -> None:  # pragma: no cover - needs real docker
        args = ["up", "-d"] + ([self._guard(service)] if service else [])
        subprocess.run(self._compose(*args), check=True, timeout=900)

    def compose_restart(self, service: str | None = None) -> None:  # pragma: no cover - needs real docker
        args = ["restart"] + ([self._guard(service)] if service else [])
        subprocess.run(self._compose(*args), check=True, timeout=600)

    def compose_down(self, service: str | None = None) -> None:  # pragma: no cover - needs real docker
        """Whole-project down when called with no service. This is the highest-blast-radius verb
        the backend exposes: it stops the entire stack, the agent and the GPU scheduler included.
        It is implemented because the protocol declares it, not because anything should call it
        casually."""
        args = ["down"] + ([self._guard(service)] if service else [])
        subprocess.run(self._compose(*args), check=True, timeout=600)

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
    def __init__(self, scheduler: Scheduler, backend: ContainerBackend, history=None):
        self.scheduler = scheduler
        self.backend = backend
        # Optional LeaseHistory sink — the durable record of lease outcomes (the pure scheduler
        # keeps only live state). Wall clocks are stamped by the sink, here in the shell.
        self.history = history

    def reconcile(self) -> None:
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
        for name in self.scheduler.take_restorable():
            self.backend.start(name)

    def request(self, job: Job) -> None:
        if self.history:
            self.history.submitted(job.id, job.kind, job.vram_gb)
        self.scheduler.submit(job)
        self.reconcile()
        if self.history and job.id in self.scheduler.status()["rejected"]:
            self.history.rejected(job.id)

    def complete(self, job_id: str) -> None:
        if self.history:
            self.history.ended(job_id, "completed")
        self.scheduler.complete(job_id)
        self.backend.stop(job_id)
        self.reconcile()

    def heartbeat(self, job_id: str) -> bool:
        """Renew a running job's lease (liveness-based). No reconcile — nothing starts or stops."""
        return self.scheduler.heartbeat(job_id)

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
        self.reconcile()
        return expired
