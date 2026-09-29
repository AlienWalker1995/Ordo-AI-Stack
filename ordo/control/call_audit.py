"""What the audit record of an ops-controller call says, and writing it.

Every state-changing call (AUDITED_METHODS) leaves one record whatever its outcome, and so does a
read of another project's logs (`audited_read`). `ControlPlane.handle` decides when; this module
decides what: the action and target a call names (`audit_subject`, which reads only the one body
field that names the target), the caller's self-declared name (`audit_actor`), how it ended
(`audit_result`) and, for a lease request, how the scheduler answered (`lease_detail`).
`CallAuditor` writes the records to the rotating JSONL log (ordo/control/audit.py) and reads them
back for `GET /audit`.
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .audit import AuditLog
from .responses import confirmed, error

logger = logging.getLogger(__name__)

# Every call with one of these methods changes state (or asks to), so it leaves one audit record
# whatever its outcome. GET/HEAD never do; a read would flood the log (Hermes polls).
AUDITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
AUDIT_READ_LIMIT_MAX = 1000
# Who asked, as the caller names itself. Unauthenticated callers can send anything, so the value
# is reduced to a short, safe token before it is written.
ACTOR_HEADER = "x-actor"
_ACTOR_UNSAFE = re.compile(r"[^A-Za-z0-9_.:@-]")
_ACTOR_MAX = 64
_AUDIT_FIELD_MAX = 200
_AUDIT_ERROR_MAX = 300
# (path prefix, path suffix, action): the target is the path segment between them.
_AUDIT_PATH_VERBS = (
    ("/services/", "/start", "start"),
    ("/services/", "/stop", "stop"),
    ("/services/", "/restart", "restart"),
    ("/services/", "/recreate", "recreate"),
    ("/containers/", "/restart", "container.restart"),
    ("/plugins/", "/enable", "plugin.enable"),
    ("/plugins/", "/disable", "plugin.disable"),
    ("/registry/models/", "/assign-gpu", "gpu_assign"),
)
# path: (action, the one body field that names the target).
_AUDIT_BODY_VERBS = {
    "/model-config": ("model_config", "model"),
    "/jobs": ("lease.request", "id"),
    "/jobs/complete": ("lease.release", "id"),
    "/jobs/heartbeat": ("lease.heartbeat", "id"),
    "/compose/up": ("compose.up", "service"),
    "/compose/down": ("compose.down", "service"),
    "/compose/restart": ("compose.restart", "service"),
    "/models/download": ("models.download", "filename"),
    "/comfyui/install-node-requirements": ("comfyui_pip_install", "node_path"),
    "/gpu/assign": ("gpu_assign", "service"),
}
# path: (action, target) for a call that acts on the whole rendered stack.
_AUDIT_FIXED_VERBS = {
    "/apply": ("apply", "stack"),
}

# POST /projects/{project}/containers/{name}/restart: a managed project's container (managed.py).
_PROJECT_RESTART = re.compile(r"^/projects/([^/]+)/containers/([^/]+)/restart$")
# GET /projects/{project}/containers/{name}/logs: another project's logs. The one READ that is
# audited: log output is that project's data, so every read of it leaves a record.
_PROJECT_LOGS = re.compile(r"^/projects/([^/]+)/containers/([^/]+)/logs$")


def _clip(value: Any, limit: int = _AUDIT_FIELD_MAX) -> str:
    return str(value)[:limit]


def audit_actor(header: str | None) -> str:
    """The caller's self-declared name from `X-Actor`, made safe to log; 'unknown' when absent."""
    actor = _ACTOR_UNSAFE.sub("", (header or "").strip())[:_ACTOR_MAX]
    return actor or "unknown"


def audited_read(method: str, path: str) -> bool:
    return method.upper() == "GET" and _PROJECT_LOGS.match(path) is not None


def audit_subject(path: str, body: Any) -> tuple[str, str]:
    """(action, target) for a state-changing call. Reads only the one body field that names the
    target, so nothing else a caller sends (a URL's query string, a credential) reaches the log."""
    project_restart = _PROJECT_RESTART.match(path)
    if project_restart:
        return "project.restart", _clip(f"{project_restart.group(1)}/{project_restart.group(2)}")
    project_logs = _PROJECT_LOGS.match(path)
    if project_logs:
        return "project.logs", _clip(f"{project_logs.group(1)}/{project_logs.group(2)}")
    for prefix, suffix, action in _AUDIT_PATH_VERBS:
        if path.startswith(prefix) and path.endswith(suffix) and len(path) > len(prefix) + len(suffix):
            return action, _clip(path[len(prefix):-len(suffix)])
    if path in _AUDIT_FIXED_VERBS:
        return _AUDIT_FIXED_VERBS[path]
    if path not in _AUDIT_BODY_VERBS:
        return "unknown", ""
    action, field = _AUDIT_BODY_VERBS[path]
    fields = body if isinstance(body, dict) else {}
    target = str(fields.get(field) or "").strip()
    if action == "models.download" and not target:
        # The file name the download would use: the URL's last path segment, never its query.
        target = urlparse(str(fields.get("url") or "")).path.rsplit("/", 1)[-1]
    return action, _clip(target)


def audit_result(status: int) -> str:
    if status < 400:
        return "ok"
    return "refused" if status < 500 else "error"


def lease_detail(path: str, body: Any, status: int, payload: Any) -> str | None:
    """How the scheduler answered a lease request: 'granted', 'queued', or 'rejected' (a job
    the card can never hold)."""
    if path != "/jobs" or status != 200 or not isinstance(payload, dict) or not isinstance(body, dict):
        return None
    job_id = str(body.get("id"))
    running = {str(job.get("id")) for job in payload.get("running") or [] if isinstance(job, dict)}
    if job_id in running:
        return "granted"
    if job_id in {str(rejected) for rejected in payload.get("rejected") or []}:
        return "rejected"
    return "queued"


class CallAuditor:
    """Writes one record per audited call to the audit log and reads the newest back.

    The log is opened on first use, at the path `log_path()` returns then (the control plane reads
    its configured location at that moment, so a test can point it elsewhere first); it creates its
    directory on the first write only."""

    def __init__(self, log_path: Callable[[], Path]):
        self._log_path = log_path
        self._log: AuditLog | None = None
        self._init_lock = threading.Lock()

    def _sink(self) -> AuditLog:
        # Built once, under a lock: two instances would each hold their own write lock on one file.
        with self._init_lock:
            if self._log is None:
                self._log = AuditLog(self._log_path())
            return self._log

    def record(
        self,
        method: str,
        path: str,
        body: Any,
        actor: str,
        status: int,
        error: str | None = None,
        detail: str | None = None,
        principal: str | None = None,
    ) -> None:
        """Write the one record for a state-changing call.

        The record holds only named fields (see `audit_subject`): never the request body, the
        headers or a credential. `actor` is the caller's own X-Actor claim; `principal` is what its
        token proved (ordo/control/principals.py), set by the HTTP binding on every record it
        writes. Never raises: an audit failure must not fail the action, but it is logged so a
        broken log does not go unnoticed.
        """
        fields = body if isinstance(body, dict) else {}
        action, target = audit_subject(path, body)
        extra: dict[str, Any] = {
            "method": method.upper(),
            "path": _clip(path),
            "status": status,
            "dry_run": bool(fields.get("dry_run")),
            "confirm": confirmed(fields),
        }
        if error:
            extra["error"] = _clip(error, _AUDIT_ERROR_MAX)
        if detail:
            extra["detail"] = _clip(detail)
        if principal:
            extra["principal"] = principal
        try:
            self._sink().record(action=action, target=target, result=audit_result(status),
                                caller=actor, **extra)
        except Exception:
            logger.exception("ops-controller could not write the audit record for %s %s", method, path)

    def tail(self, limit: int = 50) -> dict[str, Any]:
        """The newest `limit` audit records, newest first, across the rotated generations."""
        try:
            return {"entries": self._sink().tail(limit)}
        except OSError as e:
            return {"entries": [], "error": f"failed to read audit log: {e}"}

    def read(self, query: dict[str, str]) -> dict[str, Any]:
        """`GET /audit?limit=N`: the newest N records, N from 1 to AUDIT_READ_LIMIT_MAX (default 50)."""
        try:
            limit = int(query.get("limit", "50"))
        except ValueError:
            return error(422, "limit must be an integer")
        if not 1 <= limit <= AUDIT_READ_LIMIT_MAX:
            return error(422, f"limit must be between 1 and {AUDIT_READ_LIMIT_MAX}")
        return self.tail(limit)
