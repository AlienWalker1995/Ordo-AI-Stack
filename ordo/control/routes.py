"""The ops-controller route table: which ControlPlane handler answers each (method, path).

`ControlPlane.route` walks ROUTES in order and the first entry whose method and pattern match
answers; nothing matching is a 404. Each entry says, as data, what used to be spread over an
if-chain:

  - `pattern`: which paths it serves and which path segments it hands the handler;
  - `handler`: a function of (the control plane, the request) returning the payload. It looks the
    ControlPlane method up on every call, so a test that replaces one on an instance is honoured;
  - `exclusive`: the verb changes the stack, so it runs holding the operation lock and is refused
    with 409 while another holds it (`ControlPlane._exclusive`);
  - `always_ok`: the handler never refuses, so its payload is answered with 200 as returned.
    Every other payload carries its status in-band (`_status`, see `ControlPlane._as_response`).

Two pattern kinds exist because the paths have two shapes:

  - `Template`: a literal path, or one with `{name}` placeholders that each match exactly one
    non-empty path segment (the rule the principal allowlist uses, ordo/control/principals.py);
  - `Span`: a prefix and a suffix around a captured id that may itself contain slashes, matched
    by `startswith`/`endswith` and sliced out (`/services/{id}/start` serves `/services/a/b/start`
    with id `a/b`, and the backend refuses that name). `single_segment` narrows it to an id
    without a slash.

`template` on a pattern is its path with every placeholder written `{}`: what the test that
checks the principal allowlist against this table compares.

The table is pure data over pure functions: no framework types, so `route()` stays testable
without a server (see the module docstring of ordo/control/api.py).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .api import ControlPlane


@dataclass(frozen=True)
class Request:
    """One call as a handler sees it: the path parameters a pattern captured, the JSON body and the
    query string (both already defaulted to {})."""

    path: str
    body: dict[str, Any]
    query: dict[str, str]
    params: dict[str, str]


_PLACEHOLDER = re.compile(r"\{[^}]*\}")


@dataclass(frozen=True)
class Template:
    """A path whose `{name}` placeholders each match exactly one non-empty segment."""

    path: str
    regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        parts = []
        for segment in self.path.split("/"):
            if segment.startswith("{") and segment.endswith("}"):
                parts.append(f"(?P<{segment[1:-1]}>[^/]+)")
            else:
                parts.append(re.escape(segment))
        object.__setattr__(self, "regex", re.compile("/".join(parts)))

    @property
    def template(self) -> str:
        return _PLACEHOLDER.sub("{}", self.path)

    def match(self, path: str) -> dict[str, str] | None:
        found = self.regex.fullmatch(path)
        return found.groupdict() if found else None


@dataclass(frozen=True)
class Span:
    """`prefix` + an id + `suffix`, the id sliced out of the path as `path[len(prefix):-len(suffix)]`.

    Only `startswith(prefix)` and `endswith(suffix)` are checked, exactly as the handlers have always
    been matched, so the id may be empty or carry slashes; `single_segment` refuses one with a slash."""

    prefix: str
    name: str
    suffix: str = ""
    single_segment: bool = False

    @property
    def template(self) -> str:
        return f"{self.prefix}{{}}{self.suffix}"

    def match(self, path: str) -> dict[str, str] | None:
        if not (path.startswith(self.prefix) and path.endswith(self.suffix)):
            return None
        captured = path[len(self.prefix):len(path) - len(self.suffix)]
        if self.single_segment and "/" in captured:
            return None
        return {self.name: captured}


Handler = Callable[["ControlPlane", Request], dict[str, Any]]


@dataclass(frozen=True)
class Route:
    method: str
    pattern: Template | Span
    handler: Handler
    exclusive: bool = False
    always_ok: bool = False

    def match(self, method: str, path: str) -> dict[str, str] | None:
        """The captured path parameters when this entry serves (method, path), else None.
        `method` is already upper-case."""
        if method != self.method:
            return None
        return self.pattern.match(path)


# Order matters only where two patterns of one method could both match a path; the first wins.
# GET /metrics is not here: the HTTP binding serves it as plain text (ControlPlane.app).
ROUTES: tuple[Route, ...] = (
    # Status and the drift-safe source writes.
    Route("GET", Template("/status"), lambda cp, r: cp.status(), always_ok=True),
    Route("GET", Template("/model-config"), lambda cp, r: cp.get_model_config(), always_ok=True),
    Route("POST", Template("/model-config"), lambda cp, r: cp.set_model_config(r.body), exclusive=True),
    Route("POST", Template("/apply"), lambda cp, r: cp.apply(r.body), exclusive=True),
    Route("GET", Template("/plugins"), lambda cp, r: cp.list_plugins(), always_ok=True),
    Route("POST", Span("/plugins/", "id", "/enable"),
          lambda cp, r: cp.enable_plugin(r.params["id"], r.body), exclusive=True),
    Route("POST", Span("/plugins/", "id", "/disable"),
          lambda cp, r: cp.disable_plugin(r.params["id"], r.body), exclusive=True),
    # The GPU lease. Never exclusive: a heartbeat must answer while a recreate runs.
    Route("POST", Template("/jobs"), lambda cp, r: cp.request_job(r.body)),
    Route("POST", Template("/jobs/complete"), lambda cp, r: cp.complete_job(r.body)),
    Route("POST", Template("/jobs/heartbeat"), lambda cp, r: cp.heartbeat_job(r.body)),
    Route("GET", Template("/jobs/history"), lambda cp, r: cp.jobs_history(), always_ok=True),
    Route("GET", Template("/doctor"), lambda cp, r: cp.doctor(), always_ok=True),
    Route("GET", Template("/health"), lambda cp, r: cp.health(), always_ok=True),
    Route("GET", Template("/healthz"), lambda cp, r: cp.health(), always_ok=True),
    # Service lifecycle.
    Route("POST", Span("/services/", "id", "/start"),
          lambda cp, r: cp.service_start(r.params["id"], r.body), exclusive=True),
    Route("POST", Span("/services/", "id", "/stop"),
          lambda cp, r: cp.service_stop(r.params["id"], r.body), exclusive=True),
    Route("POST", Span("/services/", "id", "/restart"),
          lambda cp, r: cp.service_restart(r.params["id"], r.body), exclusive=True),
    Route("GET", Span("/services/", "id", "/logs"), lambda cp, r: cp.service_logs(r.params["id"])),
    Route("GET", Template("/services"), lambda cp, r: cp.list_services()),
    Route("POST", Span("/services/", "id", "/recreate"),
          lambda cp, r: cp.service_recreate(r.params["id"], r.body), exclusive=True),
    # Containers of the Ordo project. The logs route precedes the inspect route it would shadow.
    Route("GET", Template("/containers"), lambda cp, r: cp.list_containers()),
    Route("GET", Span("/containers/", "name", "/logs"), lambda cp, r: cp.container_logs(r.params["name"])),
    Route("POST", Span("/containers/", "name", "/restart"),
          lambda cp, r: cp.container_restart(r.params["name"], r.body), exclusive=True),
    Route("GET", Span("/containers/", "name", single_segment=True),
          lambda cp, r: cp.container_inspect(r.params["name"])),
    Route("GET", Template("/stats/services"), lambda cp, r: cp.service_stats()),
    # Compose verbs; no `service` in the body means the whole stack.
    Route("POST", Template("/compose/up"), lambda cp, r: cp.compose_up(r.body), exclusive=True),
    Route("POST", Template("/compose/down"), lambda cp, r: cp.compose_down(r.body), exclusive=True),
    Route("POST", Template("/compose/restart"), lambda cp, r: cp.compose_restart(r.body), exclusive=True),
    # What the render serves, and the live GPUs.
    Route("GET", Template("/registry/models"), lambda cp, r: cp.registry_models(), always_ok=True),
    Route("GET", Template("/registry/gpus"), lambda cp, r: cp.registry_gpus(), always_ok=True),
    Route("GET", Template("/gpus"), lambda cp, r: cp.live_gpus(), always_ok=True),
    # ComfyUI model downloads.
    Route("POST", Template("/models/download"), lambda cp, r: cp.models_download(r.body)),
    Route("GET", Template("/models/download/status"), lambda cp, r: cp.models_download_status(), always_ok=True),
    # Diagnostics and the audit log.
    Route("GET", Template("/diagnostics/dstate"), lambda cp, r: cp.diagnostics_dstate(), always_ok=True),
    Route("GET", Template("/audit"), lambda cp, r: cp.read_audit(r.query)),
    # ComfyUI node requirements, and the two retired GPU-pin routes (an honest 410).
    Route("POST", Template("/comfyui/install-node-requirements"),
          lambda cp, r: cp.comfyui_install_node_requirements(r.body), exclusive=True),
    Route("POST", Template("/gpu/assign"), lambda cp, r: cp.gpu_assign_gone(r.body.get("service", ""))),
    Route("POST", Span("/registry/models/", "model", "/assign-gpu"),
          lambda cp, r: cp.gpu_assign_gone(r.path.split("/")[3])),
    # Managed projects: status, logs and a confirmed restart of OTHER compose projects.
    Route("GET", Template("/projects"), lambda cp, r: cp.managed_projects_overview()),
    Route("GET", Template("/projects/{project}/containers"),
          lambda cp, r: cp.managed_project_containers(r.params["project"])),
    Route("GET", Template("/projects/{project}/containers/{name}/logs"),
          lambda cp, r: cp.managed_container_logs(r.params["project"], r.params["name"], r.query)),
    Route("POST", Template("/projects/{project}/containers/{name}/restart"),
          lambda cp, r: cp.managed_container_restart(r.params["project"], r.params["name"], r.body)),
)


def find(method: str, path: str) -> tuple[Route, dict[str, str]] | None:
    """The first entry serving (method, path) with the parameters it captured, or None."""
    upper = method.upper()
    for entry in ROUTES:
        params = entry.match(upper, path)
        if params is not None:
            return entry, params
    return None
