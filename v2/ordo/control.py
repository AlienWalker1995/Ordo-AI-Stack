"""Control plane — the ops-controller service, as pure request handlers.

This is what the rendered compose's `ops-controller` service runs. It exposes the substrate
over HTTP: the live GPU/scheduler status the dashboard and agents poll, a re-render endpoint,
and the drift-safe model switch.

Design constraints (from the architecture decisions + the drift lessons):
  - ONE write path. Changing the active model does NOT hand-edit `.env` or a separate registry;
    it writes the *declarative source* (`ordo.yaml`) and re-renders. `.env` is always a pure
    function of the source, so a runtime model switch can never drift the three ctx values apart.
  - The handlers are pure (method, path, body) -> (status, dict) so they're testable with no
    server/socket. `serve()` is a thin stdlib http.server binding around `route()` (no-cover).
  - No auth here: the dashboard is localhost-only and this is the full control plane behind it
    (the agreed model — auth is Caddy's job at the edge, not baked into every service).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .broker import Broker
from .catalog import Catalog
from .config import Source
from .plugins import PluginRegistry
from .render import render
from .scheduler import Job, Scheduler


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
    ):
        self.source_path = Path(source_path)
        self.catalog = catalog
        self.registry = registry
        self.out_dir = Path(out_dir)
        self.scheduler = scheduler
        self.broker = broker
        self.history = history  # LeaseHistory sink (shared with the broker) — /jobs/history

    # --- core operations (pure, testable) ---
    def _render(self) -> Any:
        return render(Source.load(self.source_path), self.catalog, self.registry)

    def status(self) -> dict[str, Any]:
        """Live status: GPU/scheduler state + the current rendered manifest."""
        rc = self._render()
        out: dict[str, Any] = {"manifest": rc.manifest()}
        out["gpu"] = self.scheduler.status() if self.scheduler else {"state": "no-scheduler"}
        return out

    def get_model_config(self) -> dict[str, Any]:
        src = Source.load(self.source_path)
        rc = self._render()
        return {
            "source_model": src.model,           # what the source asks for ("auto" or an id)
            "active_model": rc.model.id,          # what best-fit/override actually resolved to
            "tier": rc.tier,
            "ctx_size": rc.ctx_size,
            "available": [
                {"id": m.id, "tier": m.tier, "vram_gb": m.vram_gb} for m in self.catalog.models
            ],
        }

    def set_model_config(self, body: dict[str, Any]) -> dict[str, Any]:
        """Switch the active model the drift-safe way: write the SOURCE, then re-render.

        `.env`, Hermes context, and model-gateway ctx are all regenerated from the new source in
        one pass — they cannot end up disagreeing. `model: "auto"` hands control back to best-fit.
        """
        model_id = str(body.get("model", "")).strip()
        if not model_id:
            return self._error(400, "body must include 'model' (a catalog id or 'auto')")
        if model_id != "auto" and self.catalog.get(model_id) is None:
            ids = [m.id for m in self.catalog.models]
            return self._error(404, f"model '{model_id}' not in catalog", available=ids)

        # ONE write path: mutate only the model key of the raw source, preserving everything else.
        raw = yaml.safe_load(self.source_path.read_text(encoding="utf-8")) or {}
        raw["model"] = model_id
        self.source_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

        rc = self._render()
        rc.write(self.out_dir)  # regenerate .env + compose + hermes ctx + manifest from the source
        return {"ok": True, "active_model": rc.model.id, "ctx_size": rc.ctx_size,
                "warnings": rc.warnings, "wrote": str(self.out_dir)}

    def request_job(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        try:
            job = Job(id=str(body["id"]), vram_gb=float(body["vram_gb"]),
                      kind=str(body.get("kind", "generic")),
                      est_seconds=float(body.get("est_seconds", 0.0)))
        except (KeyError, ValueError, TypeError):
            return self._error(400, "job needs 'id' and numeric 'vram_gb'")
        self.broker.request(job)
        return self.scheduler.status()

    def complete_job(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        job_id = str(body.get("id", "")).strip()
        if not job_id:
            return self._error(400, "body must include 'id'")
        self.broker.complete(job_id)
        return self.scheduler.status()

    def heartbeat_job(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        job_id = str(body.get("id", "")).strip()
        if not job_id:
            return self._error(400, "body must include 'id'")
        if not self.broker.heartbeat(job_id):
            return self._error(404, f"no running job '{job_id}'")
        return self.scheduler.status()

    # --- routing (also pure) ---
    def route(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict]:
        body = body or {}
        m = method.upper()
        if m == "GET" and path == "/status":
            return 200, self.status()
        if m == "GET" and path == "/model-config":
            return 200, self.get_model_config()
        if m == "POST" and path == "/model-config":
            return self._as_response(self.set_model_config(body))
        if m == "POST" and path == "/jobs":
            return self._as_response(self.request_job(body))
        if m == "POST" and path == "/jobs/complete":
            return self._as_response(self.complete_job(body))
        if m == "POST" and path == "/jobs/heartbeat":
            return self._as_response(self.heartbeat_job(body))
        if m == "GET" and path == "/jobs/history":
            # Finished leases, newest first — what the orchestration tab's history table shows.
            return 200, {"history": self.history.tail(100) if self.history else []}
        if m == "GET" and path in ("/health", "/healthz"):
            return 200, {"ok": True}
        return 404, {"error": f"no route {method} {path}"}

    @staticmethod
    def _error(status: int, message: str, **extra: Any) -> dict[str, Any]:
        return {"_status": status, "error": message, **extra}

    @staticmethod
    def _as_response(payload: dict[str, Any]) -> tuple[int, dict]:
        status = int(payload.pop("_status", 200)) if isinstance(payload, dict) else 200
        return status, payload

    def serve(self, host: str = "0.0.0.0", port: int = 9000) -> None:  # pragma: no cover - needs a socket
        """Thin stdlib http.server binding around route(). No third-party dep by design."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        cp = self

        class Handler(BaseHTTPRequestHandler):
            def _dispatch(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    status, payload = 400, {"error": "invalid JSON body"}
                else:
                    status, payload = cp.route(method, self.path.split("?")[0], body)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def log_message(self, *_a: Any) -> None:
                pass  # quiet; the agent/dashboard poll frequently

        ThreadingHTTPServer((host, port), Handler).serve_forever()
