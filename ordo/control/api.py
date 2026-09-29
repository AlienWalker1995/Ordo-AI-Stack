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
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from ..render import alerting, substrate
from ..render.catalog import Catalog
from ..render.config import Source
from ..render.models_volume import CHAT_SERVICE
from ..render.open_webui_probe import OPEN_WEBUI_SERVICE, open_webui_verdict
from ..render.plugins import PluginRegistry
from ..render.served_models import model_files
from ..render.source_edit import edit_plugins_list
from . import diagnostics, gpus, routes
from . import metrics as prom
from . import principals as auth
from .apply import RenderApply
from .broker import Broker
from .call_audit import ACTOR_HEADER, AUDITED_METHODS, CallAuditor, audit_actor, audited_read, lease_detail
from .comfyui import ModelDownloads, NodeRequirements
from .gpus import LeaseJobs
from .lifecycle import LeaseGuard, Lifecycle
from .managed_projects import ManagedProjects
from .responses import CONFIRM_REQUIRED, as_response, confirmed, error
from .scheduler import Scheduler
from .source import StackSource, enabled_ids

logger = logging.getLogger(__name__)

# The only paths reachable without the bearer token: container healthchecks carry no credentials,
# and Prometheus scrapes /metrics without one (it holds no secret: ordo/control/metrics.py).
METRICS_PATH = "/metrics"
UNAUTHENTICATED_PATHS = frozenset({"/health", "/healthz", METRICS_PATH})

COMFYUI_MODELS_DIR = Path(os.environ.get("COMFYUI_MODELS_DIR", "/models/comfyui"))
AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "/data/audit.jsonl"))
COMFYUI_CUSTOM_NODES_DIR = Path(os.environ.get("COMFYUI_CUSTOM_NODES_DIR", "/comfyui-app/ComfyUI/custom_nodes"))
COMFYUI_CONTAINER_NAME = os.environ.get("COMFYUI_CONTAINER_NAME", "ordo-comfyui-1")


# Service plugins Hermes may install/enable on request (kind=service, profile-gated). The core
# substrate (llamacpp, litellm-db, model-gateway, model-gateway-keys, ops-controller, dashboard,
# agent), the edge / front-door (edge, tailnet-names — secret-dependent, host `make up` only), and
# the agent itself are NOT here, so they can never be created/removed via this path — the
# allowlist is the security gate. Every kind=mcp plugin (an agent tool server) is installable as
# well, derived from its manifest kind (see `_installable`): the dashboard's MCP toggle persists
# through this same write path.
INSTALLABLE_PLUGINS = frozenset({
    "comfyui", "song-gen", "voice", "rag", "open-webui", "monitoring",
    "automation", "searxng-web", "codebase-memory-ui", "obsidian-livesync", "llamacpp-cpu",
})


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
    ):
        # The source and out/, and what this process renders them with. `model_volume_files` lists
        # the file names in the models volume (None: it could not be listed); None = no volume to
        # check (a control plane without the Docker socket, and the unit tests that do not wire one).
        self.source = StackSource(Path(source_path), catalog, registry, Path(out_dir), substrate.current_digest())
        # What GET /metrics reports beyond the scheduler and the containers: {mount label: a path on
        # that filesystem} and {cert name: a PEM file}. Empty = not reported (ordo/control/serve.py
        # wires the real ones).
        self.disk_paths = dict(disk_paths or {})
        self.tls_cert_files = dict(tls_cert_files or {})
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
        self.lease = LeaseGuard(scheduler)
        self.lifecycle = Lifecycle(broker, self.lease)
        self.applier = RenderApply(self.source, broker, self.lease, model_volume_files)
        self.model_volume_files = model_volume_files
        self.lease_jobs = LeaseJobs(broker, scheduler)
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

    # --- Status, the model, plugins and the post-render apply (source.py, apply.py) ---

    def _render(self) -> Any:
        return self.source.render()

    def doctor(self) -> dict[str, Any]:
        """`GET /doctor`: the drift `ordo doctor` reports, seen from the control plane. Read-only.

        Each check is judged by the function `ordo doctor` uses, never a copy: this process's
        substrate digest against the one the last render recorded in out/manifest.json
        (`substrate.substrate_verdict`; the host compares the running ops-controller with its
        checkout, which this process cannot see), and the open-webui probe (`open_webui_verdict`),
        run once in its container when it is running. `detail` is the CLI's report line without
        its "! " finding marker. The dashboard's Overview shows the failed checks.
        """
        checks = [("substrate", *self._substrate_drift()), (OPEN_WEBUI_SERVICE, *self._open_webui_drift()),
                  ("alerting", *self._alerting_drift())]
        rows = [{"check": name, "ok": ok, "detail": line.removeprefix("! ")} for name, ok, line in checks]
        return {"ok": all(row["ok"] for row in rows), "checks": rows}

    def _substrate_drift(self) -> tuple[bool, str]:
        try:
            recorded = self.source.recorded_substrate_digest()
        except (OSError, ValueError, AttributeError) as e:
            return False, f"! substrate: cannot read {self.source.out_dir / 'manifest.json'} ({e})"
        if recorded is None:
            return True, f"substrate: ops-controller {self.substrate_digest[:12]}; out/ records no digest yet"
        return substrate.substrate_verdict(self.substrate_digest, recorded, reference_name="the last render",
                                           rebuild_from="the checkout that rendered out/")

    def _alerting_drift(self) -> tuple[bool, str]:
        """The alert-delivery verdict `ordo doctor` gives, from the secret files materialized in out/."""
        try:
            compose = self._render().compose_dict()
        except Exception as e:  # noqa: BLE001 - an unrenderable source is reported, not raised
            return False, f"! alerting: cannot render the source to check alert delivery ({type(e).__name__}: {e})"
        return alerting.check(compose, self.source.out_dir)

    def _open_webui_drift(self) -> tuple[bool, str]:
        """The open-webui verdict `ordo doctor` gives: "not running" is fine, a failed probe is not."""
        if not self.broker:
            return False, "! open-webui: cannot be checked: this control plane has no container backend"
        try:
            rows = self.broker.backend.list_services().get("services", [])
        except Exception as e:  # noqa: BLE001 - an unreadable stack is reported, not raised
            return False, f"! open-webui: cannot read the running services ({type(e).__name__}: {e})"
        running = any(row.get("id") == OPEN_WEBUI_SERVICE and row.get("state") == "running" for row in rows)
        return self.applier.open_webui_verdict() if running else open_webui_verdict(None)

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

    def get_model_config(self) -> dict[str, Any]:
        src = Source.load(self.source.path)
        rc = self.source.render()
        mmproj = rc.env.get("LLAMACPP_MMPROJ") or ""
        return {
            "source_model": src.model,           # what the source asks for ("auto" or an id)
            "active_model": rc.model.id,          # what best-fit/override actually resolved to
            # The GGUF the resolved model serves. Consumers that key by file (throughput
            # attribution, the dashboard's installed check) need this, not the catalog id.
            "active_file": rc.model.file,
            # The vision projector llama.cpp loads beside it (a bare file name, like active_file).
            "active_mmproj": mmproj.rsplit("/", 1)[-1] or None,
            # Every file a rendered service loads (chat model + projector, CPU fallback, embed):
            # the dashboard's delete guard protects exactly these.
            "model_files": [{"file": f.file, "service": f.service, "optional": f.optional}
                            for f in model_files(rc.compose_dict(), rc.env)],
            "tier": rc.tier,
            "ctx_size": rc.ctx_size,
            "available": [
                {"id": m.id, "tier": m.tier, "vram_gb": m.vram_gb, "file": m.file}
                for m in self.source.catalog.models
            ],
        }

    def set_model_config(self, body: dict[str, Any]) -> dict[str, Any]:
        """Switch the active model the drift-safe way: write the SOURCE, re-render, then apply.

        `.env`, Hermes context, and model-gateway ctx are all regenerated from the new source in
        one pass — they cannot end up disagreeing. `model: "auto"` hands control back to best-fit.
        The render decides what restarts (`RenderApply.apply_render`): llama.cpp and the gateway, the CPU
        fallback and the agent when the context window changed, whatever else the render touched.
        The response's `apply` says what was recreated and what the host must finish.
        """
        model_id = str(body.get("model", "")).strip()
        if not model_id:
            return error(400, "body must include 'model' (a catalog id or 'auto')")
        if model_id != "auto" and self.source.catalog.get(model_id) is None:
            ids = [m.id for m in self.source.catalog.models]
            return error(404, f"model '{model_id}' not in catalog", available=ids)
        conflict = self.source.substrate_conflict()
        if conflict:
            return conflict

        # ONE write path: mutate only the model key of the raw source, preserving everything else.
        raw = yaml.safe_load(self.source.read_text()) or {}
        raw["model"] = model_id
        rc = self.source.render_source(Source.from_dict(raw))
        missing = self._missing_model_files(rc)
        if missing:
            return missing
        applied, failure = self.applier.commit(yaml.safe_dump(raw, sort_keys=False), rc)
        if failure:
            return failure
        return {"ok": True, "active_model": rc.model.id, "ctx_size": rc.ctx_size,
                "warnings": rc.warnings, "wrote": str(self.source.out_dir), "apply": applied}

    def _missing_model_files(self, target: Any) -> dict[str, Any] | None:
        """A refusal when the chat service would load a file the models volume lacks, else None.

        Checked before the source is written: the post-render step recreates llama.cpp right after
        a switch, and onto a missing weights file it crash-loops (a missing projector leaves it running
        without vision while model-gateway advertises vision). The fix is deliberately NOT a
        download from here: tens of GB is the host's `ordo fetch` (resumable, preflighted for
        disk), and a download inside this process would die with every ops-controller recreate."""
        if self.model_volume_files is None:
            return None
        present = self.model_volume_files()
        if present is None:
            return error(503, "cannot list the models volume to confirm the model's files are in "
                              "place; not switching")
        needed = [f for f in model_files(target.compose_dict(), target.env) if f.service == CHAT_SERVICE]
        missing = [need.file for need in needed if need.file not in present]
        if not missing:
            return None
        command = f"ordo fetch {target.model.id}"
        verb = "is" if len(missing) == 1 else "are"
        return error(409, f"{', '.join(missing)} {verb} not in the models volume: run `{command}` on "
                          "the host, then switch again", missing_files=missing, fetch_command=command)

    def _installable(self, plugin_id: str) -> bool:
        """The allowlisted service plugins plus every kind=mcp plugin in the registry."""
        if plugin_id in INSTALLABLE_PLUGINS:
            return True
        plugin = self.source.registry.get(plugin_id)
        return plugin is not None and plugin.kind == "mcp"

    def _installable_ids(self) -> list[str]:
        return sorted(p.id for p in self.source.registry.plugins if self._installable(p.id))

    def _deps_closure(self, plugin_id: str, already: set[str]) -> list[str]:
        """`plugin_id` + its transitive `depends_on` not already enabled — the set that must be added
        to the plugins list so the target resolves (the dep gate drops a plugin whose deps are off)."""
        need: list[str] = []
        seen = set(already)
        stack = [plugin_id]
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            need.append(pid)
            p = self.source.registry.get(pid)
            if p:
                stack.extend(d for d in p.depends_on if d not in seen)
        return need

    def _plugin_view(self, p: Any, enabled: set[str], present: set[str], hw: Any) -> dict[str, Any]:
        return {
            "id": p.id, "name": p.name, "description": p.description,
            "services": [s.name for s in p.services],
            "compose_profile": p.compose_profile,
            "secrets": list(p.secrets),
            "missing_secrets": [k for k in p.secrets if k not in present],
            "fits": p.fits(hw),
            "enabled": p.id in enabled,
        }

    def list_plugins(self) -> dict[str, Any]:
        """The installable-service catalog for the agent skill: each allowlisted plugin with its
        services, compose profile, secret keys, hardware fit, and whether it's already enabled."""
        rc = self.source.render()
        enabled = enabled_ids(rc)
        present = self.source.secrets_present()
        return {"plugins": [
            self._plugin_view(p, enabled, present, rc.hardware)
            for p in self.source.registry.plugins if self._installable(p.id)
        ]}

    def enable_plugin(self, plugin_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Enable a service plugin the drift-safe way (same one-write-path as a model switch): add
        it (+ any unmet deps) to ordo.yaml's `plugins:` list, re-render, regenerate out/, then apply
        (`RenderApply.apply_render`), which creates its services. Under `plugins: auto` a fitting plugin is
        ALREADY rendered, so nothing is written and the apply alone creates whatever of it is not
        running. A service whose secrets out/secrets.env lacks is rendered but left to the host.
        Refuses anything not installable (`_installable`), and anything that doesn't fit the hardware."""
        if not self._installable(plugin_id):
            return error(403, f"'{plugin_id}' is not an installable service (core, edge/"
                         "front-door, and the agent are refused)",
                         installable=self._installable_ids())
        plugin = self.source.registry.get(plugin_id)
        if plugin is None:
            return error(404, f"plugin '{plugin_id}' is not in the registry")
        if body.get("dry_run"):
            return {"would": "enable", "plugin": plugin_id}
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        src = Source.load(self.source.path)
        rc = self.source.render()
        hw = rc.hardware
        services = [s.name for s in plugin.services]
        present = self.source.secrets_present()

        if plugin_id in enabled_ids(rc):
            # Already rendered (the common case under plugins: auto): no source edit, only the apply.
            applied = self.applier.apply_render() if self.applier.broker else None
            if applied is not None and "_status" in applied:
                return applied
            return {"ok": True, "already_rendered": True, "plugin": plugin_id,
                    "services": services, "compose_profile": plugin.compose_profile,
                    "wants_secrets": bool(plugin.secrets),
                    "missing_secrets": [k for k in plugin.secrets if k not in present],
                    "warnings": [], "apply": applied}

        if not plugin.fits(hw):
            _, notes = self.source.registry.resolve([plugin_id], hw)
            reason = next((n for n in notes if plugin_id in n),
                          f"'{plugin_id}' does not fit this hardware")
            return error(409, reason)

        missing_site_keys = plugin.missing_site_keys(src.site)
        if missing_site_keys:
            return error(409, f"'{plugin_id}' needs site key(s) {', '.join(missing_site_keys)}: "
                         "set them under `site:` in ordo.yaml, then render")

        if src.plugins == "auto" or src.plugins is None:
            # fits + auto but not enabled -> a dependency was gated off (dropped by the dep fixpoint)
            _, notes = self.source.registry.resolve([plugin_id], hw)
            reason = next((n for n in notes if plugin_id in n),
                          f"'{plugin_id}' could not be enabled (an unmet dependency)")
            return error(409, reason)

        # explicit plugin list: add the plugin + any unmet deps, VALIDATE the render, then persist.
        to_add = self._deps_closure(plugin_id, enabled_ids(rc))
        blocked = [pid for pid in to_add if not self._installable(pid)]
        if blocked:
            return error(409, f"'{plugin_id}' requires {blocked}, which are not installable")
        text = self.source.read_text()
        try:
            for pid in to_add:
                text = edit_plugins_list(text, pid, "add")
        except ValueError as e:
            return error(422, f"cannot safely edit ordo.yaml plugins list: {e}")
        edited = Source.from_dict(yaml.safe_load(text))
        rc2 = self.source.render_source(edited)
        if plugin_id not in enabled_ids(rc2):
            return error(409, f"'{plugin_id}' still not enabled after the edit (unmet "
                         "dependency or fit) — nothing written")
        conflict = self.source.substrate_conflict()
        if conflict:
            return conflict
        # commit: ONE write path: the source text, then every derived output, then the apply.
        applied, failure = self.applier.commit(text, rc2)
        if failure:
            return failure
        return {"ok": True, "already_rendered": False, "plugin": plugin_id,
                "services": services, "compose_profile": plugin.compose_profile,
                "wants_secrets": bool(plugin.secrets),
                "missing_secrets": [k for k in plugin.secrets if k not in self.source.secrets_present()],
                "added": to_add, "warnings": rc2.warnings, "wrote": str(self.source.out_dir), "apply": applied}

    def disable_plugin(self, plugin_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Remove a service plugin from an EXPLICIT plugins list, re-render, then apply (symmetric to
        enable): the plugin's services, which the render no longer defines, are stopped, and whatever
        the render changed for the rest (the gateway, for an MCP server) is recreated. Under
        `plugins: auto` there is no list item to remove, so a disable could not persist: it is
        refused with the fix (an explicit list) and nothing is stopped."""
        if not self._installable(plugin_id):
            return error(403, f"'{plugin_id}' is not an installable service")
        plugin = self.source.registry.get(plugin_id)
        if plugin is None:
            return error(404, f"plugin '{plugin_id}' is not in the registry")
        if body.get("dry_run"):
            return {"would": "disable", "plugin": plugin_id}
        if not confirmed(body):
            return error(400, CONFIRM_REQUIRED)
        services = [s.name for s in plugin.services]
        src = Source.load(self.source.path)
        if src.plugins == "auto" or src.plugins is None:
            return error(409, "ordo.yaml has `plugins: auto`, which enables every fitting plugin: "
                              f"'{plugin_id}' cannot be disabled without an explicit `plugins:` list "
                              "there. Nothing was changed.", plugin=plugin_id)
        text = self.source.read_text()
        try:
            new_text = edit_plugins_list(text, plugin_id, "remove")
        except ValueError as e:
            return error(422, f"cannot safely edit ordo.yaml plugins list: {e}")
        if new_text == text:
            return {"ok": True, "already_absent": True, "plugin": plugin_id, "services": services}
        conflict = self.source.substrate_conflict()
        if conflict:
            return conflict
        edited = Source.from_dict(yaml.safe_load(new_text))
        rc2 = self.source.render_source(edited)
        applied, failure = self.applier.commit(new_text, rc2)
        if failure:
            return failure
        return {"ok": True, "plugin": plugin_id, "services": services, "wrote": str(self.source.out_dir),
                "apply": applied}

    def apply_render(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Bring the stack to the current render: recreate exactly the changed set (apply.py)."""
        return self.applier.apply_render(dry_run=dry_run)

    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.applier.apply(body)

    # --- The GPU lease and the GPU views (ordo/control/gpus.py) ---

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

    # --- Service lifecycle (ordo/control/lifecycle.py) ---

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

    # --- Managed projects: OTHER compose projects (ordo/control/managed_projects.py) ---

    def managed_projects_overview(self) -> dict[str, Any]:
        return self.managed_projects.overview()

    def managed_project_containers(self, project: str) -> dict[str, Any]:
        return self.managed_projects.containers(project)

    def managed_container_logs(self, project: str, name: str, query: dict[str, str]) -> dict[str, Any]:
        return self.managed_projects.container_logs(project, name, query)

    def managed_container_restart(self, project: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.managed_projects.container_restart(project, name, body, leased_gpu_uuid=self._leased_gpu_uuid,
                                                       gpu_indexes=self._gpu_indexes)

    # --- ComfyUI model downloads (ordo/control/comfyui.py) ---

    def models_download(self, body: dict[str, Any]) -> dict[str, Any]:
        """Start a resumable file download to the ComfyUI models directory."""
        return self.downloads.start(body, worker=self._run_model_download)

    def models_download_status(self) -> dict[str, Any]:
        return self.downloads.status()

    def _run_model_download(self, url: str, category: str, filename: str) -> None:
        self.downloads.run(url, category, filename)

    # --- Diagnostics (ordo/control/diagnostics.py) ---

    def diagnostics_dstate(self) -> dict[str, Any]:
        return diagnostics.dstate()

    # --- Audit ---

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

    def comfyui_install_node_requirements(self, body: dict[str, Any] | None) -> dict[str, Any]:
        return self.node_requirements.install(body)

    def audit_log(self, limit: int = 50) -> dict[str, Any]:
        """The newest `limit` audit records, newest first, across the rotated generations."""
        return self.auditor.tail(limit)

    def metrics_text(self) -> str:
        """GET /metrics: the lease, container, disk and certificate state in the Prometheus text
        format (ordo/control/metrics.py). Each source is read on its own; one that fails is reported
        as a failed collector and the rest are still served. Read-only."""
        containers = restarts = None
        if self.broker:
            try:
                containers = self.broker.backend.list_services().get("services", [])
            except Exception as e:  # noqa: BLE001 - an unreadable docker is a failed collector, not a 500
                logger.warning("metrics: cannot list the services: %s", e)
            try:
                restarts = self.broker.backend.service_restarts()
            except Exception as e:  # noqa: BLE001 - same
                logger.warning("metrics: cannot read the restart counts: %s", e)
        disks: dict[str, prom.DiskUsage | None] = {}
        for mount, path in self.disk_paths.items():
            try:
                disks[mount] = prom.DiskUsage.of(path)
            except OSError as e:
                logger.warning("metrics: cannot stat %s (%s): %s", path, mount, e)
                disks[mount] = None
        certs: dict[str, float | None] = {}
        for name, path in self.tls_cert_files.items():
            try:
                certs[name] = prom.read_cert_not_after(path)
            except (OSError, ValueError) as e:
                logger.warning("metrics: cannot read the %s certificate: %s", name, e)
                certs[name] = None
        return prom.render(prom.Inputs(
            scheduler=self.scheduler.status() if self.scheduler else None,
            containers=containers, restarts=restarts, disks=disks, tls_certs=certs))

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

    # --- routing (also pure) ---
    def health(self) -> dict[str, Any]:
        """The container healthcheck. The digest is not secret; `ordo doctor` reads it here to compare
        with the checkout."""
        return {"ok": True, "substrate_digest": self.substrate_digest}

    def read_audit(self, query: dict[str, str]) -> dict[str, Any]:
        return self.auditor.read(query)

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
