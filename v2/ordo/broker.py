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

import subprocess
from typing import Protocol

from .scheduler import Job, Scheduler


class ContainerBackend(Protocol):
    def start(self, job_id: str) -> None: ...
    def stop(self, name: str) -> None: ...


class MockBackend:
    """Records actions instead of touching Docker — used by tests."""
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []

    def start(self, job_id: str) -> None:
        self.started.append(job_id)

    def stop(self, name: str) -> None:
        self.stopped.append(name)


class DockerBackend:
    """Real backend, HARD-SCOPED to a compose project.

    A name passed here is a compose SERVICE name (e.g. `llamacpp`), NOT a raw container name.
    The actual container is resolved by compose LABELS
    (`com.docker.compose.project=<project>` + `com.docker.compose.service=<name>`), which is robust
    to the `-1` replica suffix compose appends (the live defect: `docker stop ordo-v2-llamacpp`
    failed because the container is `ordo-v2-llamacpp-1`). Resolving by label — the same mechanism
    ops-api uses — is exact and, because the project label is pinned, structurally unable to touch a
    container outside this project.

    A name that resolves to NO container in this project is a NO-OP (not an error): an abstract lease
    job (e.g. a Hermes media lease that runs its own ComfyUI workflow) has no container of its own —
    it only reserves/releases VRAM. Residents (llama.cpp) always resolve, so evict/restore act on the
    real container. The `_guard` still refuses any name that would escape the project prefix.
    """
    def __init__(self, project: str = "ordo-v2"):
        self.project = project

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
        container = self._resolve(service)
        if container is None:
            return  # abstract lease job — nothing to start (caller owns its workload)
        subprocess.run(["docker", "start", container], check=True, timeout=60)

    def stop(self, service: str) -> None:  # pragma: no cover - needs real docker
        container = self._resolve(service)
        if container is None:
            return  # abstract lease job — no container to stop
        subprocess.run(["docker", "stop", container], check=True, timeout=60)


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
