"""The service lifecycle verbs of the Ordo project, and the GPU-lease check they make.

start, stop, restart and recreate a service (with its netns members), restart or inspect one
container, the compose verbs, and the reads beside them (services, containers, logs, stats).

The lifecycle verbs live in the same process as the GPU scheduler, so they ask it before starting
anything (`LeaseGuard`). Starting an evicted resident during a lease (directly, by container name,
or through a whole-stack compose up) puts two tenants on one card: the 2026-08-08 host crash.
Refusing here, once, replaces a "not during a lease" check in every caller. Stop is never refused,
and a lease tenant (comfyui) may still be cycled by its own gate.
"""
from __future__ import annotations

import re
from typing import Any

from ..render.stack import lifecycle_group
from .broker import Broker
from .responses import CONFIRM_REQUIRED, confirmed, error
from .scheduler import Scheduler


class LifecycleGroupUnknown(Exception):
    """The rendered compose could not be read, so a service's netns members are unknown."""


class LeaseGuard:
    """Whether starting a service would put a second tenant on the leased card."""

    def __init__(self, scheduler: Scheduler | None):
        self.scheduler = scheduler

    def conflict(self, service: str | None) -> dict[str, Any] | None:
        """A 409 payload when starting `service` (None = the whole stack) would share the card."""
        if not self.scheduler:
            return None
        status = self.scheduler.status()
        holders = [job["id"] for job in status["running"]] + [job["id"] for job in status["queued"]]
        evicted = status["evicted_residents"]
        if service is None and status["leased"]:
            return error(409, f"a GPU lease is active (held by {holders}, evicted {sorted(evicted)}); "
                              "a whole-stack start would restart the evicted residents beside it. "
                              "Name a service, or retry once the lease is released.",
                         lease_holders=holders)
        if service is not None and service in evicted:
            return error(409, f"{service!r} is evicted for a GPU lease held by {holders}; starting it "
                              "would put two tenants on one card. The scheduler restores it when "
                              "the lease is released.", lease_holders=holders)
        return None

    def container_conflict(self, container: str) -> dict[str, Any] | None:
        """`conflict` for a raw container name (`<project>-<service>-<n>`)."""
        if not self.scheduler:
            return None
        for service in self.scheduler.evicted_residents:
            if re.fullmatch(rf"[\w.-]+-{re.escape(service)}-\d+", container):
                return self.conflict(service)
        return None

    def group_conflict(self, group: list[str]) -> dict[str, Any] | None:
        """`conflict` for each service a verb would start."""
        for service in group:
            conflict = self.conflict(service)
            if conflict:
                return conflict
        return None


class Lifecycle:
    """The lifecycle verbs and reads, over the broker's container backend. Every verb that starts
    something asks `lease` first; with no broker every call answers 503."""

    def __init__(self, broker: Broker | None, lease: LeaseGuard):
        self.broker = broker
        self.lease = lease

    # A service's netns members (`network_mode: service:<it>`) share its network namespace, so
    # every verb that gives it a new namespace must cycle them too, after it; otherwise they keep
    # running in the dead one with only `lo` (observed 2026-09-24: a caddy restart from the
    # dashboard cut off hermes-dashboard and every tailnet sidecar). The group comes from
    # `stack.lifecycle_group`, the planner the host's `ordo up` / `ordo recreate` use.

    def rendered_compose(self, target: str) -> dict:
        try:
            return self.broker.backend.rendered_compose()
        except Exception as e:
            raise LifecycleGroupUnknown(
                f"cannot read the rendered compose to find {target!r}'s netns members ({e}); "
                "refusing rather than orphaning them") from e

    def lifecycle_group(self, service: str) -> list[str]:
        """[service, *its netns members]. Raises LifecycleGroupUnknown when the render is unreadable."""
        return lifecycle_group(self.rendered_compose(service), service)

    def container_members(self, container: str) -> list[str]:
        """The netns members that follow a raw container (`<project>-<service>-<n>`), or []."""
        doc = self.rendered_compose(container)
        # Longest name first, so `ordo-tailnet-chat-1` is tailnet-chat even if a `chat` exists.
        for service in sorted(doc.get("services") or {}, key=len, reverse=True):
            if re.fullmatch(rf"[\w.-]+-{re.escape(service)}-\d+", container):
                return lifecycle_group(doc, service)[1:]
        return []

    def compose_service(self, body: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        """(service, error) for a compose verb's body. No `service` key, or null, means the whole
        stack. Anything else must be a non-empty string: `""` must never widen a named verb into a
        whole-stack down."""
        service = body.get("service")
        if service is None:
            return None, None
        if not isinstance(service, str) or not service.strip():
            return None, error(400, f"service must be a non-empty compose service name, got {service!r}; "
                                    "omit it to act on the whole stack")
        return service, None

    def restart_members(self, members: list[str]) -> None:
        """Restart each member, after its owner has its new namespace. A restart, not a start:
        a member left running while the owner was down is still in the dead namespace, and
        `docker start` of a running container does nothing."""
        for member in members:
            try:
                self.broker.backend.restart(member)
            except Exception as e:
                raise RuntimeError(f"netns member {member!r} was not restarted and has no network "
                                   f"until it is: {e}") from e

    @staticmethod
    def with_members(payload: dict[str, Any], members: list[str]) -> dict[str, Any]:
        if members:
            payload["members"] = members
        return payload

    def service_start(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        if body.get("dry_run"):
            return {"would": "start", "service": service_id}
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        try:
            group = self.lifecycle_group(service_id)
        except LifecycleGroupUnknown as e:
            return error(500, str(e))
        conflict = self.lease.group_conflict(group)
        if conflict:
            return conflict
        try:
            self.broker.backend.start(service_id)
            self.restart_members(group[1:])
        except Exception as e:
            return error(500, str(e))
        return self.with_members({"ok": True, "service": service_id, "action": "started"}, group[1:])

    def service_stop(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        if body.get("dry_run"):
            return {"would": "stop", "service": service_id}
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        try:
            group = self.lifecycle_group(service_id)
        except LifecycleGroupUnknown as e:
            return error(500, str(e))
        try:
            # Members first: stopping only the owner leaves them running in a dead namespace.
            for member in group[1:]:
                self.broker.backend.stop(member)
            self.broker.backend.stop(service_id)
        except Exception as e:
            return error(500, str(e))
        return self.with_members({"ok": True, "service": service_id, "action": "stopped"}, group[1:])

    def service_restart(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        if body.get("dry_run"):
            return {"would": "restart", "service": service_id}
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        try:
            group = self.lifecycle_group(service_id)
        except LifecycleGroupUnknown as e:
            return error(500, str(e))
        conflict = self.lease.group_conflict(group)
        if conflict:
            return conflict
        try:
            self.broker.backend.restart(service_id)
            self.restart_members(group[1:])
        except Exception as e:
            return error(500, str(e))
        return self.with_members({"ok": True, "service": service_id, "action": "restarted"}, group[1:])

    def service_logs(self, service_id: str, tail: int = 100) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        try:
            logs = self.broker.backend.logs(service_id, tail=tail)
        except Exception as e:
            return error(500, str(e))
        return {"logs": logs, "service": service_id}

    def list_services(self) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        try:
            services = self.broker.backend.list_services()
        except Exception as e:
            return error(500, str(e))
        # The backend returns the ops-api payload already ({"services": [...]}), the same as
        # list_containers below. Wrapping it again here produced
        # {"services": {"services": [...]}}, which the dashboard would read as an empty grid.
        return services

    def service_recreate(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        if body.get("dry_run"):
            return {"would": "recreate", "service": service_id}
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        try:
            group = self.lifecycle_group(service_id)
        except LifecycleGroupUnknown as e:
            return error(500, str(e))
        conflict = self.lease.group_conflict(group)
        if conflict:
            return conflict
        try:
            # One compose call recreates the whole group: the backend plans it with the same
            # `stack.plan_named` the host's `ordo recreate` uses.
            self.broker.backend.recreate_service(service_id)
        except Exception as e:
            return error(500, str(e))
        return self.with_members({"ok": True, "service": service_id, "action": "recreated"}, group[1:])

    def list_containers(self) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        try:
            containers = self.broker.backend.list_containers()
        except Exception as e:
            return error(500, str(e))
        return containers

    def container_logs(self, name: str, tail: int = 100) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        try:
            logs = self.broker.backend.container_logs(name, tail=tail)
        except Exception as e:
            return error(500, str(e))
        return logs

    def container_inspect(self, name: str) -> dict[str, Any]:
        """One Ordo container through `broker.summarize_inspect`'s field allowlist: never its
        environment or labels. 404 for a name that is not a container of this project."""
        if not self.broker:
            return error(503, "no broker configured")
        try:
            return self.broker.backend.container_inspect(name)
        except ValueError as e:
            return error(404, str(e))
        except Exception as e:
            return error(500, str(e))

    def container_restart(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        try:
            members = self.container_members(name)
        except LifecycleGroupUnknown as e:
            return error(500, str(e))
        conflict = self.lease.container_conflict(name) or self.lease.group_conflict(members)
        if conflict:
            return conflict
        try:
            self.broker.backend.container_restart(name)
            self.restart_members(members)
        except Exception as e:
            return error(500, str(e))
        return self.with_members({"ok": True, "container": name, "action": "restarted"}, members)

    def service_stats(self) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        try:
            stats = self.broker.backend.service_stats()
        except Exception as e:
            return error(500, str(e))
        return stats

    def compose_up(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        service, invalid = self.compose_service(body)
        if invalid:
            return invalid
        try:
            # A named compose verb acts on the service's whole lifecycle group (the backend
            # expands it through `bringup`), so every member is lease-checked too.
            conflict = (self.lease.group_conflict(self.lifecycle_group(service)) if service
                        else self.lease.conflict(None))
        except LifecycleGroupUnknown as e:
            return error(500, str(e))
        if conflict:
            return conflict
        try:
            self.broker.backend.compose_up(service)
        except Exception as e:
            return error(500, str(e))
        return {"ok": True, "action": "compose-up"}

    def compose_down(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        service, invalid = self.compose_service(body)
        if invalid:
            return invalid
        try:
            # Down REMOVES containers: an evicted resident taken down has nothing left for the
            # scheduler to restore, and a whole-stack down ends the lease holder's work too.
            conflict = (self.lease.group_conflict(self.lifecycle_group(service)) if service
                        else self.lease.conflict(None))
        except LifecycleGroupUnknown as e:
            return error(500, str(e))
        if conflict:
            return conflict
        try:
            self.broker.backend.compose_down(service)
        except Exception as e:
            return error(500, str(e))
        return {"ok": True, "action": "compose-down"}

    def compose_restart(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return error(503, "no broker configured")
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        service, invalid = self.compose_service(body)
        if invalid:
            return invalid
        try:
            # A named compose verb acts on the service's whole lifecycle group (the backend
            # expands it through `bringup`), so every member is lease-checked too.
            conflict = (self.lease.group_conflict(self.lifecycle_group(service)) if service
                        else self.lease.conflict(None))
        except LifecycleGroupUnknown as e:
            return error(500, str(e))
        if conflict:
            return conflict
        try:
            self.broker.backend.compose_restart(service)
        except Exception as e:
            return error(500, str(e))
        return {"ok": True, "action": "compose-restart"}
