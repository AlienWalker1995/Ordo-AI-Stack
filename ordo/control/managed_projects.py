"""The routes that maintain OTHER compose projects on this host: status, logs and a restart.

The list is the source's `managed_projects:`, read per call so an edit takes effect without a
restart. Ordo's own project is refused by the source validation and again by the backend, so no
Ordo service, and so no lease-managed resident, is reachable here. The policy these routes apply
(the GPU guard, the restart budget, the row fields) is ordo/control/managed.py.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..render.config import Source
from . import managed
from .broker import Broker
from .responses import CONFIRM_REQUIRED, confirmed, error


def _project_rows(containers: list[dict]) -> list[dict]:
    """Each managed-project row reduced to managed.ROW_FIELDS: no field a backend adds leaks."""
    return [{field: row.get(field) for field in managed.ROW_FIELDS} for row in containers]


class ManagedProjects:
    """Status, logs and a confirmed, rate-limited restart of the listed projects' containers."""

    def __init__(self, broker: Broker | None, source_path: Path):
        self.broker = broker
        self.source_path = source_path
        # Restarts of managed-project containers: at most 3 per container per hour (managed.py).
        self.restart_budget = managed.RestartBudget()

    def refusal(self, project: str) -> dict[str, Any] | None:
        """A 404 payload unless `project` is listed in the source's `managed_projects:`."""
        if not self.broker:
            return error(503, "no broker configured")
        try:
            listed = Source.load(self.source_path).managed_projects
        except Exception as e:  # noqa: BLE001 - an unreadable source manages nothing
            return error(500, f"cannot read managed_projects from the source: {e}")
        if project not in listed:
            return error(404, f"{project!r} is not a managed project (ordo.yaml managed_projects: {listed})")
        return None

    def overview(self) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        try:
            listed = Source.load(self.source_path).managed_projects
        except Exception as e:  # noqa: BLE001
            return error(500, f"cannot read managed_projects from the source: {e}")
        projects = []
        for project in listed:
            try:
                containers = self.broker.backend.foreign_containers(project)
                projects.append({"project": project, "containers": _project_rows(containers)})
            except Exception as e:  # noqa: BLE001 - one unreadable project does not hide the others
                projects.append({"project": project, "containers": [], "error": str(e)})
        return {"projects": projects}

    def containers(self, project: str) -> dict[str, Any]:
        refusal = self.refusal(project)
        if refusal:
            return refusal
        try:
            containers = self.broker.backend.foreign_containers(project)
        except ValueError as e:
            return error(404, str(e))
        except Exception as e:
            return error(500, str(e))
        return {"project": project, "containers": _project_rows(containers)}

    def container_logs(self, project: str, name: str, query: dict[str, str]) -> dict[str, Any]:
        refusal = self.refusal(project)
        if refusal:
            return refusal
        try:
            tail = int(query.get("tail", managed.LOG_TAIL_DEFAULT))
        except ValueError:
            return error(422, "tail must be an integer")
        tail = max(1, min(tail, managed.LOG_TAIL_MAX))
        try:
            logs = self.broker.backend.foreign_logs(project, name, tail)
        except ValueError as e:
            return error(404, str(e))
        except Exception as e:
            return error(500, str(e))
        return {"project": project, "container": name, "tail": tail, "logs": logs}

    def container_restart(self, project: str, name: str, body: dict[str, Any],
                          leased_gpu_uuid: Callable[[], str | None],
                          gpu_indexes: Callable[[], dict[str, str]]) -> dict[str, Any]:
        """A confirmed, rate-limited restart that never puts a second tenant on the leased card.
        `leased_gpu_uuid` and `gpu_indexes` are asked only once the container is known."""
        refusal = self.refusal(project)
        if refusal:
            return refusal
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        key = f"{project}/{name}"
        try:
            raw = self.broker.backend.foreign_inspect(project, name)
        except ValueError as e:
            return error(404, str(e))
        except Exception as e:
            return error(500, str(e))
        gpu_refusal = managed.gpu_refusal(raw, leased_gpu_uuid(), gpu_indexes())
        if gpu_refusal:
            return error(409, f"refusing to restart {key}: {gpu_refusal}")
        # Reserve the slot BEFORE restarting (one lock: parallel callers cannot all pass the check).
        # Give it back ONLY when the backend's guard refused (ValueError, raised before docker ran).
        # Any other failure may come after the container already restarted (`docker restart` timing
        # out waiting for it, or exiting non-zero), so the slot stays spent: better one restart
        # under-allowed than restarts nobody counted.
        wait = self.restart_budget.reserve(key)
        if wait is not None:
            return error(429, f"{key} was restarted {self.restart_budget.limit} times in the last hour; "
                              "a restart loop needs a diagnosis, not another restart",
                         retry_after_seconds=wait)
        try:
            self.broker.backend.foreign_restart(project, name)
        except ValueError as e:
            self.restart_budget.refund(key)
            return error(404, str(e))
        except Exception as e:
            return error(500, str(e))
        return {"ok": True, "project": project, "container": name, "action": "restarted"}
