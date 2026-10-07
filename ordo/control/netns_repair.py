"""Bring back network-namespace members that Docker left stopped after a daemon restart.

A container declared `network_mode: service:<owner>` joins its owner's network namespace when it
starts, so it can only start while the owner runs. Compose orders that on `up`, and the renderer
couples the member's restart to the owner's (ordo/render/compose.py), but a Docker daemon restart
bypasses compose: the daemon starts every `restart: unless-stopped` container itself, in no
dependency order, and a member that comes up before its owner fails with "cannot join network
namespace of a non running container" (State.Error). The restart policy does not retry a container
that failed to START, so it stays down until someone notices.

That happened on 2026-10-06: after an engine restart all 8 tailnet sidecars stayed down for 43
hours while every service behind them was healthy. The sidecars no longer share a namespace
(services/tailnet-names), but some members must: hermes-dashboard binds loopback inside Caddy's
namespace, and a VPN client's members (a managed project's gluetun) share its namespace by design,
so their traffic cannot leave any other way. This sweep is the safety net for those.

It repairs a member only when all of these hold, so it never fights an operator:
  - the member is stopped and its last start failed to join the namespace (Docker's own error), not
    stopped on purpose (`docker stop` leaves no such error);
  - its restart policy is not "no" (a one-shot is meant to stay down);
  - its owner is running now (otherwise starting it would fail again);
  - its repair budget allows it: at most 3 per container per hour (ordo/control/managed.py).
Ordo's own members are recreated through compose (`recreate_service`), which also re-resolves an
owner that was recreated under a new container id. A managed project's member is restarted.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any

from .managed import RestartBudget

# The error Docker records when a member's start cannot join its owner's namespace. Both forms:
# the owner exists but is not running, or the owner container the member was created against is gone.
NAMESPACE_JOIN_ERROR = "network namespace"


def is_orphan(row: dict[str, Any]) -> bool:
    """Whether a netns member row (ContainerBackend.netns_rows) is a member Docker failed to start."""
    return (row.get("state") in ("exited", "created")
            and NAMESPACE_JOIN_ERROR in str(row.get("error") or "")
            and row.get("restart_policy") != "no")


def repairable(row: dict[str, Any], *, own_project: bool) -> bool:
    """An orphan whose repair can succeed now. Ordo's own members are recreated through compose,
    which joins the CURRENT owner container, so an owner recreated under a new id ("missing") is
    fine there; a managed project's member is only restarted, which needs the very owner it was
    created against to be running."""
    if not is_orphan(row):
        return False
    owner_state = row.get("owner_state")
    return owner_state == "running" or (own_project and owner_state == "missing")


class NetnsRepair:
    """One sweep over Ordo's project and every managed project. Stateful only for the repair budget
    and the counters `/metrics` reports."""

    def __init__(self, backend: Any, own_project: str, managed_projects: Callable[[], list[str]],
                 budget: RestartBudget | None = None, log: Callable[[str], None] = print) -> None:
        self.backend = backend
        self.own_project = own_project
        self.managed_projects = managed_projects
        self.budget = budget or RestartBudget()
        self.log = log
        self.repaired = 0
        self.failed = 0
        self.orphans = 0   # orphans the LAST sweep found and could not bring back

    def stats(self) -> dict[str, int]:
        return {"repaired": self.repaired, "failed": self.failed, "orphans": self.orphans}

    def _projects(self) -> list[str]:
        try:
            listed = list(self.managed_projects())
        except Exception as exc:  # noqa: BLE001 - an unreadable source still sweeps Ordo itself
            self.log(f"[netns-repair] cannot read managed_projects ({exc}); sweeping {self.own_project} only")
            listed = []
        return [self.own_project] + [p for p in listed if p != self.own_project]

    def sweep(self) -> list[str]:
        """Repair what can be repaired now; returns the containers brought back."""
        brought_back: list[str] = []
        still_down = 0
        for project in self._projects():
            own = project == self.own_project
            try:
                rows = self.backend.netns_rows(None if own else project)
            except Exception as exc:  # noqa: BLE001 - one unreadable project does not stop the sweep
                self.log(f"[netns-repair] cannot list {project}'s containers: {exc}")
                continue
            for row in rows:
                if not is_orphan(row):
                    continue
                name = row["name"]
                if not repairable(row, own_project=own):
                    still_down += 1
                    continue
                key = f"{project}/{name}"
                if self.budget.reserve(key) is not None:
                    still_down += 1
                    self.log(f"[netns-repair] {key} is down again but its repair budget is spent "
                             f"(3 per hour); leaving it for the operator")
                    continue
                try:
                    if own:
                        self.backend.recreate_service(row["service"])
                    else:
                        self.backend.foreign_restart(project, name)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, OSError) as exc:
                    self.failed += 1
                    still_down += 1
                    detail = getattr(exc, "stderr", "") or exc
                    self.log(f"[netns-repair] could not bring back {key}: {str(detail).strip()[:300]}")
                    continue
                self.repaired += 1
                brought_back.append(key)
                self.log(f"[netns-repair] brought back {key}: it had failed to join "
                         f"{row.get('owner') or 'its owner'}'s network namespace "
                         f"({str(row.get('error')).strip()[:160]})")
        self.orphans = still_down
        return brought_back
