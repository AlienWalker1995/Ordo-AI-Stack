"""Control plane — the ops-controller service, as pure request handlers.

This is what the rendered compose's `ops-controller` service runs. It exposes the substrate
over HTTP: the live GPU/scheduler status the dashboard and agents poll, the drift-safe model
switch, plugin enable/disable, and `POST /apply`.

Design constraints (from the architecture decisions + the drift lessons):
  - ONE write path. Changing the active model does NOT hand-edit `.env` or a separate registry;
    it writes the *declarative source* (`ordo.yaml`) and re-renders. `.env` is always a pure
    function of the source, so a runtime model switch can never drift the three ctx values apart.
  - The handlers are pure (method, path, body) -> (status, dict) so they're testable with no
    server/socket. `serve()` is a thin FastAPI binding around `route()` (no-cover).
  - Every HTTP call must carry `Authorization: Bearer <OPS_CONTROLLER_TOKEN>`; only the health
    probe is open. This API can stop services, re-render the stack and hand out GPU leases, so
    network isolation alone is not enough: any compromised container on ordo-net could drive it.
    The check lives in the HTTP layer (`app()`); `route()` itself stays pure.
  - ONE post-render step. Every source write (model switch, plugin enable/disable, and so the
    dashboard's MCP toggle) is followed by `apply_render`: recreate exactly the services whose
    rendered config hash or image differs from their container's (ordo/render/changed_set.py, the
    computation the host's `ordo apply` makes), GPU-lease checked, netns members with their owner.
    What this process cannot recreate (itself, the agent calling it, a service missing its
    secrets) is returned as `restart_required_on_host` with the `ordo apply --only ...` command. A
    failed apply restores the previous source and re-applies it. No caller keeps its own list.
  - Every state-changing call (POST/PUT/PATCH/DELETE) leaves one audit record, whatever its
    outcome, refusals included. It is written in one place, `handle()` (plus the 401 and bad-JSON
    refusals in `app()`, which never reach it), so a new route is audited without opting in.

`ControlPlane` is a facade. Each concern lives in its own module and is handed exactly the shared
state it uses (the broker, the scheduler, the source, the GPU-lease check); `ControlPlane` builds
them once and keeps one method per route handler, a one-line delegate, so the route table and
every caller see one stable surface:

  routes.py            the route table `route()` dispatches through
  source.py            the operator source and out/: render, atomic write, substrate check
  apply.py             the post-render step, and the source commit that rolls back on failure
  model_config.py      GET/POST /model-config
  plugin_install.py    GET /plugins, plugin enable and disable
  doctor.py            GET /doctor: the drift `ordo doctor` reports, seen from here
  lifecycle.py         the service, container and compose verbs, and the GPU-lease check they make
  gpus.py              the GPU lease routes and the GPU views
  managed_projects.py  OTHER compose projects: status, logs and a budgeted restart
  comfyui.py           ComfyUI model downloads and custom-node requirements
  diagnostics.py       the D-state scan
  metrics.py           GET /metrics (served by `app()` as plain text, outside the route table)
  call_audit.py        what an audit record says, and writing it
  responses.py         the payload conventions: error(), as_response(), confirmed()
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..render import substrate
from ..render.catalog import Catalog
from ..render.plugins import PluginRegistry
from . import diagnostics, gpus, routes
from . import metrics as prom
from . import principals as auth
from .apply import RenderApply
from .broker import Broker
from .call_audit import ACTOR_HEADER, AUDITED_METHODS, CallAuditor, audit_actor, audited_read, lease_detail
from .comfyui import ModelDownloads, NodeRequirements
from .doctor import DriftReport
from .gpus import LeaseJobs
from .lifecycle import LeaseGuard, Lifecycle
from .managed_projects import ManagedProjects
from .model_config import ModelConfig
from .plugin_install import PluginInstaller
from .residents import ResidentFootprints
from .responses import as_response, error
from .scheduler import Scheduler
from .source import StackSource

logger = logging.getLogger(__name__)

# The only paths reachable without the bearer token: container healthchecks carry no credentials,
# and Prometheus scrapes /metrics without one (it holds no secret: ordo/control/metrics.py).
METRICS_PATH = "/metrics"
UNAUTHENTICATED_PATHS = frozenset({"/health", "/healthz", METRICS_PATH})

COMFYUI_MODELS_DIR = Path(os.environ.get("COMFYUI_MODELS_DIR", "/models/comfyui"))
AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "/data/audit.jsonl"))
COMFYUI_CUSTOM_NODES_DIR = Path(os.environ.get("COMFYUI_CUSTOM_NODES_DIR", "/comfyui-app/ComfyUI/custom_nodes"))
COMFYUI_CONTAINER_NAME = os.environ.get("COMFYUI_CONTAINER_NAME", "ordo-comfyui-1")


class ControlPlane:
    def __init__(
        self,
        source_path: str | Path,
        catalog: Catalog,
        registry: PluginRegistry,
        out_dir: str | Path,
        scheduler: Scheduler | None = None,
        broker: Broker | None = None,
        history=None,
        model_volume_files: Callable[[], set[str] | None] | None = None,
        disk_paths: dict[str, str] | None = None,
        tls_cert_files: dict[str, str] | None = None,
        chat_health_url: str | None = None,
    ):
        # The source and out/, and what this process renders them with. `model_volume_files` lists
        # the file names in the models volume (None: it could not be listed); None = no volume to
        # check (a control plane without the Docker socket, and the unit tests that do not wire one).
        self.source = StackSource(Path(source_path), catalog, registry, Path(out_dir), substrate.current_digest())
        self.scheduler = scheduler
        self.broker = broker
        self.history = history  # LeaseHistory sink (shared with the broker): /jobs/history
        # Requests run concurrently on worker threads (see app()), so the verbs that change the
        # stack take this lock, one at a time (`_exclusive`). It is the broker's operation lock, the
        # one a GPU lease transition takes, so a lease cannot evict a resident mid-verb either. A
        # control plane without a broker only needs its source writes kept apart.
        self._operation_lock = broker.operation_lock if broker else threading.RLock()
        # `handle()` writes one audit record per state-changing call. AUDIT_LOG_PATH is read when
        # the log is first used, so a test can point it elsewhere after construction.
        self.auditor = CallAuditor(lambda: AUDIT_LOG_PATH)
        # One object per concern, each handed exactly the shared state it uses. The GPU-lease check
        # is shared by the lifecycle verbs and the post-render apply.
        # The scheduler's resident footprints, kept equal to the render's (residents.py).
        self.residents = ResidentFootprints(self.source, broker)
        self.lease = LeaseGuard(scheduler)
        self.lifecycle = Lifecycle(broker, self.lease)
        self.applier = RenderApply(self.source, broker, self.lease, model_volume_files,
                                   after_apply=self.residents.adopt)
        self.model_config = ModelConfig(self.source, self.applier, model_volume_files)
        self.plugins = PluginInstaller(self.source, self.applier)
        self.drift = DriftReport(self.source, self.applier, broker)
        self.lease_jobs = LeaseJobs(broker, scheduler)
        # What GET /metrics reports beyond the scheduler and the containers: {mount label: a path on
        # that filesystem} and {cert name: a PEM file}. Empty = not reported (ordo/control/serve.py
        # wires the real ones).
        self.metrics = prom.MetricsCollector(scheduler, broker, disk_paths or {}, tls_cert_files or {},
                                             chat_health_url=chat_health_url)
        self.managed_projects = ManagedProjects(broker, self.source.path)
        # ComfyUI's files. Their locations are read from this module's settings when used, so a test
        # can repoint them after construction.
        self.downloads = ModelDownloads(lambda: COMFYUI_MODELS_DIR)
        self.node_requirements = NodeRequirements(broker, lambda: COMFYUI_CUSTOM_NODES_DIR,
                                                  lambda: COMFYUI_CONTAINER_NAME)

    @property
    def source_path(self) -> Path:
        return self.source.path

    @property
    def substrate_digest(self) -> str:
        return self.source.substrate_digest

    # --- Status, the model switch, plugins, the post-render apply and the drift report (source.py,
    # model_config.py, plugin_install.py, apply.py, doctor.py) ---

    def _render(self) -> Any:
        return self.source.render()

    def status(self) -> dict[str, Any]:
        """Live status: GPU/scheduler state + the current rendered manifest."""
        rc = self._render()
        out: dict[str, Any] = {"manifest": rc.manifest()}
        out["gpu"] = self.scheduler.status() if self.scheduler else {"state": "no-scheduler"}
        if self.scheduler:
            # Whether a restart would keep the lease: the host's `ordo recreate ops-controller`
            # refuses a mid-lease recreate unless this is true.
            out["gpu"]["state_persisted"] = bool(self.broker and self.broker.state_persisted)
        return out

    def health(self) -> dict[str, Any]:
        """The container healthcheck. The digest is not secret; `ordo doctor` reads it here to compare
        with the checkout."""
        return {"ok": True, "substrate_digest": self.substrate_digest}

    def get_model_config(self) -> dict[str, Any]:
        return self.model_config.get()

    def set_model_config(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.model_config.set(body)

    def list_plugins(self) -> dict[str, Any]:
        return self.plugins.list_plugins()

    def enable_plugin(self, plugin_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.plugins.enable(plugin_id, body)

    def disable_plugin(self, plugin_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.plugins.disable(plugin_id, body)

    def apply_render(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Bring the stack to the current render: recreate exactly the changed set (apply.py)."""
        return self.applier.apply_render(dry_run=dry_run)

    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.applier.apply(body)

    def doctor(self) -> dict[str, Any]:
        """`GET /doctor`: the drift `ordo doctor` reports, seen from here (doctor.py)."""
        return self.drift.report()

    # --- The GPU lease and the GPU views (gpus.py) ---

    def request_job(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.lease_jobs.request(body)

    def complete_job(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.lease_jobs.complete(body)

    def heartbeat_job(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.lease_jobs.heartbeat(body)

    def jobs_history(self) -> dict[str, Any]:
        """Finished leases, newest first: what the orchestration tab's history table shows."""
        return {"history": self.history.tail(100) if self.history else []}

    def registry_models(self) -> dict[str, Any]:
        """Every model the current render serves, keyed by model id."""
        return {"models": self._served_models()}

    def registry_gpus(self) -> dict[str, Any]:
        """Live GPU info (gpu_live) with the models the render pins to each card."""
        return gpus.registry_gpus(self._live_gpus(), self._served_models())

    def live_gpus(self) -> dict[str, Any]:
        return gpus.live_cards()

    def gpu_assign_gone(self, target: str = "") -> dict[str, Any]:
        return gpus.assign_gone()

    def _served_models(self) -> dict[str, dict[str, Any]]:
        return gpus.served_by(self._render())

    def _live_gpus(self) -> dict[str, dict[str, Any]]:
        return gpus.live_by_uuid()

    def _leased_gpu_uuid(self) -> str | None:
        return gpus.leased_gpu_uuid(self._render)

    _gpu_indexes = staticmethod(gpus.gpu_indexes)

    # --- Service lifecycle (lifecycle.py) ---

    def service_start(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.service_start(service_id, body)

    def service_stop(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.service_stop(service_id, body)

    def service_restart(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.service_restart(service_id, body)

    def service_recreate(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.service_recreate(service_id, body)

    def service_logs(self, service_id: str, tail: int = 100) -> dict[str, Any]:
        return self.lifecycle.service_logs(service_id, tail)

    def list_services(self) -> dict[str, Any]:
        return self.lifecycle.list_services()

    def list_containers(self) -> dict[str, Any]:
        return self.lifecycle.list_containers()

    def container_logs(self, name: str, tail: int = 100) -> dict[str, Any]:
        return self.lifecycle.container_logs(name, tail)

    def container_inspect(self, name: str) -> dict[str, Any]:
        return self.lifecycle.container_inspect(name)

    def container_restart(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.container_restart(name, body)

    def service_stats(self) -> dict[str, Any]:
        return self.lifecycle.service_stats()

    def compose_up(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.compose_up(body)

    def compose_down(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.compose_down(body)

    def compose_restart(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.lifecycle.compose_restart(body)

    # --- Managed projects: OTHER compose projects (managed_projects.py) ---

    def managed_projects_overview(self) -> dict[str, Any]:
        return self.managed_projects.overview()

    def managed_project_containers(self, project: str) -> dict[str, Any]:
        return self.managed_projects.containers(project)

    def managed_container_logs(self, project: str, name: str, query: dict[str, str]) -> dict[str, Any]:
        return self.managed_projects.container_logs(project, name, query)

    def managed_container_restart(self, project: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.managed_projects.container_restart(project, name, body, leased_gpu_uuid=self._leased_gpu_uuid,
                                                       gpu_indexes=self._gpu_indexes)

    # --- ComfyUI model downloads and node requirements (comfyui.py) ---

    def models_download(self, body: dict[str, Any]) -> dict[str, Any]:
        """Start a resumable file download to the ComfyUI models directory."""
        return self.downloads.start(body, worker=self._run_model_download)

    def models_download_status(self) -> dict[str, Any]:
        return self.downloads.status()

    def _run_model_download(self, url: str, category: str, filename: str) -> None:
        self.downloads.run(url, category, filename)

    def comfyui_install_node_requirements(self, body: dict[str, Any] | None) -> dict[str, Any]:
        return self.node_requirements.install(body)

    # --- Diagnostics (diagnostics.py) and Prometheus metrics (metrics.py) ---

    def diagnostics_dstate(self) -> dict[str, Any]:
        return diagnostics.dstate()

    def metrics_text(self) -> str:
        """GET /metrics, in the Prometheus text format (metrics.py). Read-only."""
        return self.metrics.text()

    # --- The audit log (call_audit.py) ---

    def audit_call(
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
        """Write the one audit record for a state-changing call (call_audit.CallAuditor.record)."""
        self.auditor.record(method, path, body, actor, status, error, detail, principal)

    def audit_log(self, limit: int = 50) -> dict[str, Any]:
        """The newest `limit` audit records, newest first, across the rotated generations."""
        return self.auditor.tail(limit)

    def read_audit(self, query: dict[str, str]) -> dict[str, Any]:
        return self.auditor.read(query)

    # --- Routing: the audited entry point, the route table (routes.py) and the operation lock ---

    def handle(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        query: dict[str, str] | None,
        actor: str,
        principal: str | None = None,
    ) -> tuple[int, dict]:
        """`route()` plus the audit record: the HTTP binding's one entry point.

        Every call with an AUDITED_METHODS method leaves exactly one record, whatever route() does
        with it (success, dry run, 4xx refusal, 5xx failure or an exception). Reads leave none,
        except a read of another project's logs (`audited_read`).
        """
        if method.upper() not in AUDITED_METHODS and not audited_read(method, path):
            return self.route(method, path, body, query)
        try:
            status, payload = self.route(method, path, body, query)
        except Exception as e:
            self.audit_call(method, path, body, actor, 500, str(e) or type(e).__name__, principal=principal)
            raise
        error = payload.get("error") if isinstance(payload, dict) else None
        self.audit_call(method, path, body, actor, status, str(error) if error else None,
                        lease_detail(path, body, status, payload), principal=principal)
        return status, payload

    def route(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        """(status, payload) for one call, from the route table (ordo/control/routes.py)."""
        found = routes.find(method, path)
        if found is None:
            return 404, {"error": f"no route {method} {path}"}
        entry, params = found
        request = routes.Request(path=path, body=body or {}, query=query or {}, params=params)
        if entry.exclusive:
            payload = self._exclusive(lambda: entry.handler(self, request))
        else:
            payload = entry.handler(self, request)
        if entry.always_ok:
            return 200, payload
        return self._as_response(payload)

    def _exclusive(self, verb: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run a stack-changing verb holding the operation lock, or refuse it with 409 while
        another holds it (the convention for "busy", as a second download is refused). Refusing
        rather than waiting keeps a caller from queueing behind a recreate that can take 15
        minutes, past its own timeout, and from tying up a worker thread while it waits."""
        if not self._operation_lock.acquire(blocking=False):
            return self._error(409, "another operation that changes the stack is in progress (a lifecycle "
                                    "verb, a render apply or a GPU lease transition); retry when it finishes")
        try:
            return verb()
        finally:
            self._operation_lock.release()

    _error = staticmethod(error)
    _as_response = staticmethod(as_response)

    def app(self, auth_token: str | Callable[[], str] | None, scoped: Sequence[auth.Principal] = ()):
        """Build the FastAPI application that delegates every authenticated request to route().

        `auth_token` is the admin token, and it is required: without one the API would be open to
        every container on the network, so an empty token is refused here rather than silently
        served. It may be a function that reads the token (serve passes one reading the /run/secrets
        file): each request then checks against the current value, so a rotated file takes effect
        without a restart. A read that fails or comes back empty (a torn write) keeps the last good
        token, never an open API.

        `scoped` adds narrower principals (ordo/control/principals.py), each with its own token and
        route allowlist. A call whose token proves one of them, on a route it may not call, is
        refused with 403 before routing and recorded, whatever its method. Emptying a scoped
        principal's token file revokes it on the next request (TokenSource `empty_revokes`).
        """
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, Response
        from starlette.concurrency import run_in_threadpool

        read_token = auth_token if callable(auth_token) else (lambda: auth_token or "")
        admin = auth.admin(read_token)
        if not admin.token.current():
            raise ValueError("ops-controller needs OPS_CONTROLLER_TOKEN: refusing to serve an unauthenticated API")
        # Admin first: a scoped token equal to the admin token is the admin credential.
        known = (admin, *scoped)

        cp = self
        app = FastAPI(title="ops-controller")

        @app.middleware("http")
        async def dispatch(request: Request, call_next):
            method = request.method
            path = request.url.path
            actor = audit_actor(request.headers.get(ACTOR_HEADER))
            audited = method.upper() in AUDITED_METHODS
            principal = auth.authenticate(known, request.headers.get("authorization", ""))
            client = request.client.host if request.client else "unknown"
            if path not in UNAUTHENTICATED_PATHS and principal is None:
                # Never log the presented credential, right or wrong.
                logger.warning("ops-controller refused %s %s from %s: missing or invalid bearer token (401)",
                               method, path, client)
                error = "missing or invalid bearer token"
                if audited:
                    # The body of an unauthenticated call is never read, so the record has only
                    # what the method and path say.
                    cp.audit_call(method, path, None, actor, 401, error, principal=auth.UNAUTHENTICATED)
                return JSONResponse(content={"error": error}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            if path not in UNAUTHENTICATED_PATHS and not principal.allows(method, path):
                # A proven principal asking for a route it was not granted: refused before routing,
                # and recorded for every method, reads included. Its body is never read.
                error = f"the {principal.name} principal may not call {method} {path}"
                logger.warning("ops-controller refused %s %s from %s: %s (403)", method, path, client, error)
                cp.audit_call(method, path, None, actor, 403, error, principal=principal.name)
                return JSONResponse(content={"error": error}, status_code=403)
            principal_name = principal.name if principal else auth.UNAUTHENTICATED
            if path == METRICS_PATH:
                # Plain text, not route()'s JSON. A read, so never audited. It runs docker and
                # statvfs, so it goes to a worker thread like every other request.
                if method.upper() != "GET":
                    return JSONResponse(content={"error": f"no route {method} {path}"}, status_code=404)
                text = await run_in_threadpool(cp.metrics_text)
                return Response(content=text, media_type=prom.CONTENT_TYPE)
            body = None
            if method in ("POST", "PUT", "PATCH"):
                raw = await request.body()
                if raw:
                    try:
                        body = json.loads(raw)
                    except json.JSONDecodeError:
                        error = "invalid JSON body"
                        cp.audit_call(method, path, None, actor, 400, error, principal=principal_name)
                        return JSONResponse(content={"error": error}, status_code=400)
                    # Every route reads its body as an object; an array, string, number or null
                    # would otherwise fail inside a handler as a bare 500.
                    if not isinstance(body, dict):
                        error = "JSON body must be an object"
                        cp.audit_call(method, path, None, actor, 400, error, principal=principal_name)
                        return JSONResponse(content={"error": error}, status_code=400)
            # Concurrency contract: handle() runs on a worker thread, never on the event loop. A verb can
            # block on docker for up to 15 minutes, and on the loop it stalled every other request
            # (/health, /status, GPU-lease heartbeats) behind it. Requests therefore run in parallel:
            # the scheduler guards its state with its own lock, and verbs that change the stack take the
            # operation lock (`_exclusive`, a second one gets 409), which lease calls never wait for.
            status, payload = await run_in_threadpool(
                cp.handle, method, path, body, dict(request.query_params), actor, principal_name)
            return JSONResponse(content=payload, status_code=status)

        return app

    def serve(self, auth_token: str | Callable[[], str] | None, host: str = "0.0.0.0",
              port: int = 9000, scoped: Sequence[auth.Principal] = ()) -> None:  # pragma: no cover - needs a socket
        """Thin FastAPI binding around route().

        The binding is deliberately thin: every request is dispatched through route(), which stays a pure
        function with no framework types in or under it, so control-plane logic remains testable without a
        socket. FastAPI is reached through a function-local import, so importing ordo.control (which the CLI
        does for render and fetch) does not require FastAPI to be installed.

        Dispatch is an http middleware rather than a catch-all route on purpose: this module uses postponed
        annotations, and FastAPI resolves a route handler's annotations against MODULE globals, where a
        function-local `Request` does not exist. That combination silently turns `request` into a required
        query parameter and answers every call with 422. Middleware is not resolved that way.
        """
        import uvicorn

        app = self.app(auth_token, scoped)
        uvicorn.run(app, host=host, port=port, log_level="warning")
