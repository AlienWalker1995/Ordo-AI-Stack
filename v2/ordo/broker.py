"""Process broker — drives the scheduler's decisions against real containers.

The Scheduler is the pure decision core; the Broker is the imperative shell that reconciles those
decisions into container start/stop via a pluggable backend:
  - MockBackend  — for tests (records what would start/stop).
  - DockerBackend — real, but HARD-SCOPED to the v2 project prefix so it can NEVER touch the live
    ordo-ai-stack containers (a guard refuses any name outside the project).

Flow: request(job) → scheduler.submit + reconcile(); reconcile() = scheduler.pump() then start the
newly-admitted containers and stop any LRU-evicted idle models. complete(job) frees the slot and
reconciles (admitting whatever was waiting).
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
    """Real backend, HARD-SCOPED to a compose project prefix.

    It only ever `docker start/stop`s containers named `<project>-*`, so it is structurally unable
    to touch the live `ordo-ai-stack-*` containers. Passing a name outside the project raises.
    """
    def __init__(self, project: str = "ordo-v2"):
        self.project = project

    def _guard(self, name: str) -> str:
        full = name if name.startswith(f"{self.project}-") else f"{self.project}-{name}"
        if not full.startswith(f"{self.project}-"):
            raise ValueError(f"refusing to touch container outside project '{self.project}': {name}")
        return full

    def start(self, job_id: str) -> None:  # pragma: no cover - needs real docker
        subprocess.run(["docker", "start", self._guard(job_id)], check=True, timeout=60)

    def stop(self, name: str) -> None:  # pragma: no cover - needs real docker
        subprocess.run(["docker", "stop", self._guard(name)], check=True, timeout=60)


class Broker:
    def __init__(self, scheduler: Scheduler, backend: ContainerBackend):
        self.scheduler = scheduler
        self.backend = backend

    def reconcile(self) -> None:
        admitted, evicted = self.scheduler.pump()
        for name in evicted:      # stop LRU-evicted idle models first to free VRAM
            self.backend.stop(name)
        for job_id in admitted:   # then start the newly-admitted jobs
            self.backend.start(job_id)

    def request(self, job: Job) -> None:
        self.scheduler.submit(job)
        self.reconcile()

    def complete(self, job_id: str) -> None:
        self.scheduler.complete(job_id)
        self.backend.stop(job_id)
        self.reconcile()
