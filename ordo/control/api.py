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
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from ..render import alerting, gpu_live, substrate
from ..render.catalog import Catalog
from ..render.changed_set import STOPPED_STATES, Change, diff_services, stale_one_shot_jobs
from ..render.config import Source
from ..render.engine import render
from ..render.models_volume import CHAT_SERVICE, required_model_files
from ..render.open_webui_probe import OPEN_WEBUI_PROBE, OPEN_WEBUI_SERVICE, open_webui_verdict
from ..render.plugins import PluginRegistry
from ..render.served_models import model_files, models_by_gpu, served_models
from ..render.source_edit import edit_plugins_list
from ..render.stack import lifecycle_group, plan_named
from . import metrics as prom
from . import principals as auth
from . import routes
from .broker import SELF_REFERENTIAL_SERVICES, Broker
from .call_audit import ACTOR_HEADER, AUDITED_METHODS, CallAuditor, audit_actor, audited_read, lease_detail
from .comfyui import ModelDownloads, NodeRequirements
from .lifecycle import LeaseGuard, Lifecycle
from .managed_projects import ManagedProjects
from .responses import CONFIRM_REQUIRED, as_response, confirmed, error
from .scheduler import Job, Scheduler

logger = logging.getLogger(__name__)

# The only paths reachable without the bearer token: container healthchecks carry no credentials,
# and Prometheus scrapes /metrics without one (it holds no secret: ordo/control/metrics.py).
METRICS_PATH = "/metrics"
UNAUTHENTICATED_PATHS = frozenset({"/health", "/healthz", METRICS_PATH})

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
    ):
        self.source_path = Path(source_path)
        # What GET /metrics reports beyond the scheduler and the containers: {mount label: a path on
        # that filesystem} and {cert name: a PEM file}. Empty = not reported (ordo/control/serve.py
        # wires the real ones).
        self.disk_paths = dict(disk_paths or {})
        self.tls_cert_files = dict(tls_cert_files or {})
        # Lists the file names in the models volume (None: it could not be listed). A model switch
        # checks the target's files against it before writing anything. None = no volume to check
        # (a control plane without the Docker socket, and the unit tests that do not wire one).
        self.model_volume_files = model_volume_files
        self.catalog = catalog
        self.registry = registry
        self.out_dir = Path(out_dir)
        self.scheduler = scheduler
        self.broker = broker
        self.history = history  # LeaseHistory sink (shared with the broker) — /jobs/history
        # The digest of the render inputs this process ships (its baked copy, in the image).
        self.substrate_digest = substrate.current_digest()
        # ComfyUI's files. Their locations are read from this module's settings when used, so a test
        # can repoint them after construction.
        self.downloads = ModelDownloads(lambda: COMFYUI_MODELS_DIR)
        self.node_requirements = NodeRequirements(broker, lambda: COMFYUI_CUSTOM_NODES_DIR,
                                                  lambda: COMFYUI_CONTAINER_NAME)
        # `handle()` writes one audit record per state-changing call. AUDIT_LOG_PATH is read when
        # the log is first used, so a test can point it elsewhere after construction.
        self.auditor = CallAuditor(lambda: AUDIT_LOG_PATH)
        # Requests run concurrently on worker threads (see app()), so the verbs that change the
        # stack take this lock, one at a time (`_exclusive`). It is the broker's operation lock, the
        # one a GPU lease transition takes, so a lease cannot evict a resident mid-verb either. A
        # control plane without a broker only needs its source writes kept apart.
        self._operation_lock = broker.operation_lock if broker else threading.RLock()
        # The lifecycle verbs, and the GPU-lease check they and the post-render apply make.
        self.lease = LeaseGuard(scheduler)
        self.lifecycle = Lifecycle(broker, self.lease)
        self.managed_projects = ManagedProjects(broker, self.source_path)


    # --- core operations (pure, testable) ---
    def _render(self) -> Any:
        return render(Source.load(self.source_path), self.catalog, self.registry)

    def _substrate_conflict(self) -> dict[str, Any] | None:
        """A 409 payload when out/ was last rendered from different inputs than this process ships.

        Rendering over it would silently revert whatever the newer side changed (the image renders
        from its own baked copy of ordo/, catalog/ and the manifests). No manifest, or one written
        before renders recorded a digest, is allowed: this render then records ours.
        """
        try:
            recorded = self._recorded_substrate_digest()
        except (OSError, ValueError, AttributeError) as e:
            return self._error(409, f"cannot read {self.out_dir / 'manifest.json'} to check the render substrate "
                               f"({e}); re-render from the host checkout, then retry")
        if recorded is None or recorded == self.substrate_digest:
            return None
        return self._error(
            409,
            f"ops-controller's render substrate ({self.substrate_digest[:12]}) differs from the last "
            f"host render's ({str(recorded)[:12]}): the image is older or newer than the checkout that "
            "rendered out/, and a render here would silently change what that checkout rendered. "
            "Rebuild ordo/ops-controller from the checkout that rendered out/ (`ordo build "
            "ops-controller`), re-render, then `ordo recreate ops-controller`.",
            substrate_digest=self.substrate_digest, rendered_substrate_digest=recorded)

    def _recorded_substrate_digest(self) -> str | None:
        """The substrate digest the last render recorded in out/manifest.json. None when there is
        no manifest or it was written before renders recorded one. Raises OSError, ValueError or
        AttributeError when the manifest cannot be read."""
        manifest_path = self.out_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        recorded = json.loads(manifest_path.read_text(encoding="utf-8")).get("substrate_digest")
        return str(recorded) if recorded else None

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
            recorded = self._recorded_substrate_digest()
        except (OSError, ValueError, AttributeError) as e:
            return False, f"! substrate: cannot read {self.out_dir / 'manifest.json'} ({e})"
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
        return alerting.check(compose, self.out_dir)

    def _open_webui_drift(self) -> tuple[bool, str]:
        """The open-webui verdict `ordo doctor` gives: "not running" is fine, a failed probe is not."""
        if not self.broker:
            return False, "! open-webui: cannot be checked: this control plane has no container backend"
        try:
            rows = self.broker.backend.list_services().get("services", [])
        except Exception as e:  # noqa: BLE001 - an unreadable stack is reported, not raised
            return False, f"! open-webui: cannot read the running services ({type(e).__name__}: {e})"
        running = any(row.get("id") == OPEN_WEBUI_SERVICE and row.get("state") == "running" for row in rows)
        return self._open_webui_verdict() if running else open_webui_verdict(None)

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
        src = Source.load(self.source_path)
        rc = self._render()
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
                for m in self.catalog.models
            ],
        }

    def set_model_config(self, body: dict[str, Any]) -> dict[str, Any]:
        """Switch the active model the drift-safe way: write the SOURCE, re-render, then apply.

        `.env`, Hermes context, and model-gateway ctx are all regenerated from the new source in
        one pass — they cannot end up disagreeing. `model: "auto"` hands control back to best-fit.
        The render decides what restarts (`apply_render`): llama.cpp and the gateway, the CPU
        fallback and the agent when the context window changed, whatever else the render touched.
        The response's `apply` says what was recreated and what the host must finish.
        """
        model_id = str(body.get("model", "")).strip()
        if not model_id:
            return self._error(400, "body must include 'model' (a catalog id or 'auto')")
        if model_id != "auto" and self.catalog.get(model_id) is None:
            ids = [m.id for m in self.catalog.models]
            return self._error(404, f"model '{model_id}' not in catalog", available=ids)
        conflict = self._substrate_conflict()
        if conflict:
            return conflict

        # ONE write path: mutate only the model key of the raw source, preserving everything else.
        raw = yaml.safe_load(self.source_path.read_text(encoding="utf-8")) or {}
        raw["model"] = model_id
        rc = render(Source.from_dict(raw), self.catalog, self.registry)
        missing = self._missing_model_files(rc)
        if missing:
            return missing
        applied, failure = self._commit_source(yaml.safe_dump(raw, sort_keys=False), rc)
        if failure:
            return failure
        return {"ok": True, "active_model": rc.model.id, "ctx_size": rc.ctx_size,
                "warnings": rc.warnings, "wrote": str(self.out_dir), "apply": applied}

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
            return self._error(503, "cannot list the models volume to confirm the model's files are in "
                                    "place; not switching")
        needed = [f for f in model_files(target.compose_dict(), target.env) if f.service == CHAT_SERVICE]
        missing = [need.file for need in needed if need.file not in present]
        if not missing:
            return None
        command = f"ordo fetch {target.model.id}"
        verb = "is" if len(missing) == 1 else "are"
        return self._error(409, f"{', '.join(missing)} {verb} not in the models volume: run `{command}` on "
                                "the host, then switch again", missing_files=missing, fetch_command=command)

    # --- service-plugin install/enable (render authority for Hermes-driven onboarding) ---
    def _secrets_present(self) -> set[str]:
        """Secret KEYS with a non-empty value in out/secrets.env (empty if the file is absent). Lets an
        enable request tell whether a service's secrets are provisioned, so a secret-dependent service
        is escalated to a host `make up` rather than started broken."""
        p = self.out_dir / "secrets.env"
        present: set[str] = set()
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if v.strip().strip('"').strip("'"):
                    present.add(k.strip())
        return present

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
            p = self.registry.get(pid)
            if p:
                stack.extend(d for d in p.depends_on if d not in seen)
        return need

    def _installable(self, plugin_id: str) -> bool:
        """The allowlisted service plugins plus every kind=mcp plugin in the registry."""
        if plugin_id in INSTALLABLE_PLUGINS:
            return True
        plugin = self.registry.get(plugin_id)
        return plugin is not None and plugin.kind == "mcp"

    def _installable_ids(self) -> list[str]:
        return sorted(p.id for p in self.registry.plugins if self._installable(p.id))

    @staticmethod
    def _enabled_ids(rc: Any) -> set[str]:
        """Every plugin a render enabled. `plugins_enabled` lists only kind=service plugins; the
        enabled kind=mcp plugins are the ones behind its MCP servers."""
        return set(rc.plugins_enabled) | {s["plugin_id"] for s in rc.mcp_servers}

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
        rc = self._render()
        enabled = self._enabled_ids(rc)
        present = self._secrets_present()
        return {"plugins": [
            self._plugin_view(p, enabled, present, rc.hardware)
            for p in self.registry.plugins if self._installable(p.id)
        ]}

    def enable_plugin(self, plugin_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Enable a service plugin the drift-safe way (same one-write-path as set_model_config): add
        it (+ any unmet deps) to ordo.yaml's `plugins:` list, re-render, regenerate out/, then apply
        (`apply_render`), which creates its services. Under `plugins: auto` a fitting plugin is
        ALREADY rendered, so nothing is written and the apply alone creates whatever of it is not
        running. A service whose secrets out/secrets.env lacks is rendered but left to the host.
        Refuses anything not installable (`_installable`), and anything that doesn't fit the hardware."""
        if not self._installable(plugin_id):
            return self._error(403, f"'{plugin_id}' is not an installable service (core, edge/"
                               "front-door, and the agent are refused)",
                               installable=self._installable_ids())
        plugin = self.registry.get(plugin_id)
        if plugin is None:
            return self._error(404, f"plugin '{plugin_id}' is not in the registry")
        if body.get("dry_run"):
            return {"would": "enable", "plugin": plugin_id}
        if not confirmed(body):
            return self._error(400, CONFIRM_REQUIRED)
        src = Source.load(self.source_path)
        rc = self._render()
        hw = rc.hardware
        services = [s.name for s in plugin.services]
        present = self._secrets_present()

        if plugin_id in self._enabled_ids(rc):
            # Already rendered (the common case under plugins: auto): no source edit, only the apply.
            applied = self.apply_render() if self.broker else None
            if applied is not None and "_status" in applied:
                return applied
            return {"ok": True, "already_rendered": True, "plugin": plugin_id,
                    "services": services, "compose_profile": plugin.compose_profile,
                    "wants_secrets": bool(plugin.secrets),
                    "missing_secrets": [k for k in plugin.secrets if k not in present],
                    "warnings": [], "apply": applied}

        if not plugin.fits(hw):
            _, notes = self.registry.resolve([plugin_id], hw)
            reason = next((n for n in notes if plugin_id in n),
                          f"'{plugin_id}' does not fit this hardware")
            return self._error(409, reason)

        missing_site_keys = plugin.missing_site_keys(src.site)
        if missing_site_keys:
            return self._error(409, f"'{plugin_id}' needs site key(s) {', '.join(missing_site_keys)}: "
                               "set them under `site:` in ordo.yaml, then render")

        if src.plugins == "auto" or src.plugins is None:
            # fits + auto but not enabled -> a dependency was gated off (dropped by the dep fixpoint)
            _, notes = self.registry.resolve([plugin_id], hw)
            reason = next((n for n in notes if plugin_id in n),
                          f"'{plugin_id}' could not be enabled (an unmet dependency)")
            return self._error(409, reason)

        # explicit plugin list: add the plugin + any unmet deps, VALIDATE the render, then persist.
        to_add = self._deps_closure(plugin_id, self._enabled_ids(rc))
        blocked = [pid for pid in to_add if not self._installable(pid)]
        if blocked:
            return self._error(409, f"'{plugin_id}' requires {blocked}, which are not installable")
        text = self.source_path.read_text(encoding="utf-8")
        try:
            for pid in to_add:
                text = edit_plugins_list(text, pid, "add")
        except ValueError as e:
            return self._error(422, f"cannot safely edit ordo.yaml plugins list: {e}")
        edited = Source.from_dict(yaml.safe_load(text))
        rc2 = render(edited, self.catalog, self.registry)
        if plugin_id not in self._enabled_ids(rc2):
            return self._error(409, f"'{plugin_id}' still not enabled after the edit (unmet "
                               "dependency or fit) — nothing written")
        conflict = self._substrate_conflict()
        if conflict:
            return conflict
        # commit: ONE write path: the source text, then every derived output, then the apply.
        applied, failure = self._commit_source(text, rc2)
        if failure:
            return failure
        return {"ok": True, "already_rendered": False, "plugin": plugin_id,
                "services": services, "compose_profile": plugin.compose_profile,
                "wants_secrets": bool(plugin.secrets),
                "missing_secrets": [k for k in plugin.secrets if k not in self._secrets_present()],
                "added": to_add, "warnings": rc2.warnings, "wrote": str(self.out_dir), "apply": applied}

    def disable_plugin(self, plugin_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Remove a service plugin from an EXPLICIT plugins list, re-render, then apply (symmetric to
        enable): the plugin's services, which the render no longer defines, are stopped, and whatever
        the render changed for the rest (the gateway, for an MCP server) is recreated. Under
        `plugins: auto` there is no list item to remove, so a disable could not persist: it is
        refused with the fix (an explicit list) and nothing is stopped."""
        if not self._installable(plugin_id):
            return self._error(403, f"'{plugin_id}' is not an installable service")
        plugin = self.registry.get(plugin_id)
        if plugin is None:
            return self._error(404, f"plugin '{plugin_id}' is not in the registry")
        if body.get("dry_run"):
            return {"would": "disable", "plugin": plugin_id}
        if not confirmed(body):
            return self._error(400, CONFIRM_REQUIRED)
        services = [s.name for s in plugin.services]
        src = Source.load(self.source_path)
        if src.plugins == "auto" or src.plugins is None:
            return self._error(409, "ordo.yaml has `plugins: auto`, which enables every fitting plugin: "
                                    f"'{plugin_id}' cannot be disabled without an explicit `plugins:` list "
                                    "there. Nothing was changed.", plugin=plugin_id)
        text = self.source_path.read_text(encoding="utf-8")
        try:
            new_text = edit_plugins_list(text, plugin_id, "remove")
        except ValueError as e:
            return self._error(422, f"cannot safely edit ordo.yaml plugins list: {e}")
        if new_text == text:
            return {"ok": True, "already_absent": True, "plugin": plugin_id, "services": services}
        conflict = self._substrate_conflict()
        if conflict:
            return conflict
        edited = Source.from_dict(yaml.safe_load(new_text))
        rc2 = render(edited, self.catalog, self.registry)
        applied, failure = self._commit_source(new_text, rc2)
        if failure:
            return failure
        return {"ok": True, "plugin": plugin_id, "services": services, "wrote": str(self.out_dir),
                "apply": applied}

    # --- the post-render step: recreate exactly what a render changed ---

    def _commit_source(self, text: str, rendered: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Write `text` as the operator source, write its render to out/, then apply it.

        Returns (the apply result, None), or (None, a failure payload) when the apply refused or
        failed: the previous source is then written back, re-rendered and re-applied, so the source
        never names a config the stack is not running. The apply result is None when this control
        plane has no container backend (a hand-run or test instance): nothing is recreated then.
        """
        previous_text = self.source_path.read_text(encoding="utf-8")
        self._write_source(text)
        rendered.write(self.out_dir)
        if not self.broker:
            return None, None
        # A service the new render no longer defines (a disabled plugin's) is stopped by the apply.
        applied = self.apply_render()
        if "_status" not in applied:
            return applied, None
        self._write_source(previous_text)
        self._render().write(self.out_dir)
        rollback = self.apply_render()
        if rollback.pop("_status", None) is None:
            outcome = "ordo.yaml and out/ were rolled back and the previous render re-applied"
        else:
            outcome = (f"ordo.yaml and out/ were rolled back, but re-applying the previous render failed "
                       f"too ({rollback.get('error')}); run `ordo apply` on the host")
        applied["error"] = f"{applied['error']}; {outcome}"
        applied["rolled_back"] = True
        applied["rollback"] = rollback
        return None, applied

    def _write_source(self, text: str) -> None:
        """Replace the operator source atomically (a temp file, then a rename): a read-only request
        rendering it on another thread (GET /status) sees the old file or the new one, never half."""
        temp = self.source_path.with_name(self.source_path.name + ".tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, self.source_path)

    def _secret_holds(self, rc: Any) -> dict[str, list[str]]:
        """service -> the required secrets out/secrets.env lacks, for each enabled plugin's services
        (its compose services and its MCP server's). Started without them they crash-loop, so the
        post-render step leaves them to the host (`ordo secrets set`, then `ordo apply`)."""
        present = self._secrets_present()
        holds: dict[str, list[str]] = {}
        for plugin_id in sorted(self._enabled_ids(rc)):
            plugin = self.registry.get(plugin_id)
            if plugin is None:
                continue
            missing = [key for key in plugin.secrets if key not in plugin.optional_secrets and key not in present]
            if not missing:
                continue
            names = [service.name for service in plugin.services]
            names += [server["service"] for server in rc.mcp_servers
                      if server.get("plugin_id") == plugin_id and not server.get("hosted")]
            for name in names:
                held = holds.setdefault(name, [])
                held += [key for key in missing if key not in held]
        return holds

    def _model_file_holds(self, services: list[str], doc: dict[str, Any], rc: Any) -> dict[str, str]:
        """service -> why it waits for the host, for each of `services` that loads a model file the
        models volume lacks. Recreated onto a missing file it crash-loops; the host's `ordo apply
        --only <service>` fetches the file first (a download of that size does not belong in this
        process, see _missing_model_files). A volume that cannot be listed holds every loader."""
        if self.model_volume_files is None:
            return {}
        needed = [need for need in required_model_files(doc, rc.env, services) if not need.optional]
        if not needed:
            return {}
        present = self.model_volume_files()
        holds: dict[str, list[str]] = {}
        for need in needed:
            if present is None or need.file not in present:
                holds.setdefault(need.service, []).append(need.file)
        if present is None:
            return {name: "cannot list the models volume to confirm its model files are in place"
                    for name in holds}
        return {name: (f"loads {', '.join(files)}, which the models volume lacks: `ordo apply --only "
                       f"{name}` on the host fetches it first") for name, files in holds.items()}

    def _unbuilt_image_holds(self, services: list[str], rendered: dict[str, Any], rc: Any) -> dict[str, str]:
        """service -> why it waits for the host, for each of `services` whose first-party image (built
        from this checkout by `ordo build`, never published to a registry) is not in the local image
        cache. Compose would try to pull it and fail; the host's `ordo apply --only <service>` builds
        it first. A third-party image the cache lacks is left to compose, which pulls it."""
        owned = set(rc.first_party_images)
        holds: dict[str, str] = {}
        for name in services:
            service = rendered.get(name)
            if service is None or service.image_id is not None:
                continue
            repo = service.image_ref.rsplit(":", 1)[0]
            if repo in owned:
                holds[name] = (f"image {service.image_ref} is built from this checkout and is not in the "
                               f"local image cache: `ordo apply --only {name}` on the host builds it first")
        return holds

    def _host_reasons(self, changes: list[Change], doc: dict[str, Any], rc: Any,
                      rendered: dict[str, Any]) -> dict[str, str]:
        """Why each changed service this process must not recreate is left to the host."""
        secret_holds = self._secret_holds(rc)
        changed = [change.service for change in changes]
        file_holds = self._model_file_holds(changed, doc, rc)
        image_holds = self._unbuilt_image_holds(changed, rendered, rc)
        reasons: dict[str, str] = {}
        for change in changes:
            name = change.service
            if name in SELF_REFERENTIAL_SERVICES:
                reasons[name] = (f"{name} runs the control plane (or is the agent calling it) and cannot be "
                                 f"recreated through it ({'; '.join(change.reasons)})")
            elif change.incomparable:
                reasons[name] = "; ".join(change.reasons)
            elif name in secret_holds:
                reasons[name] = (f"needs secret(s) {', '.join(secret_holds[name])}, which out/secrets.env does "
                                 "not hold: `ordo secrets set <KEY>` on the host first")
            elif name in image_holds:
                reasons[name] = image_holds[name]
            elif name in file_holds:
                reasons[name] = file_holds[name]
            else:
                members = [m for m in lifecycle_group(doc, name)[1:] if m in SELF_REFERENTIAL_SERVICES]
                if members:
                    reasons[name] = f"its netns member(s) {', '.join(members)} run the control plane"
        return reasons

    def apply_render(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Bring the stack to the current render: recreate exactly the changed set.

        The changed set is every long-running rendered service whose config hash or image differs
        from its container's, or that has none (ordo/render/changed_set.py, what the host's `ordo
        apply` computes). It is recreated in one `up -d --no-deps --force-recreate` call with each
        owner's netns members, after the GPU-lease check every lifecycle verb makes (an evicted
        resident is refused, 409). A service the render no longer defines that is not already stopped is stopped
        (`stopped`) unless it is already stopped (`orphans`). A one-shot job's stopped container the render moved past is removed,
        never started (`removed_jobs`; `run --rm` creates a fresh one), and a running one is left
        alone (`running_jobs`), as the host's `ordo apply` does. Left to the host, and named with the
        command that finishes the job: the control plane itself and the agent calling it, a container another compose version
        created (its hash is not comparable), a service whose secrets are missing, one whose
        first-party image is not built yet, and one that would load a model file the models volume
        lacks (the host's apply builds and fetches them). Fails closed:
        a state that cannot be read refuses (503) and recreates nothing.

        `warnings` lists what the apply could not vouch for, without undoing it: when open-webui was
        recreated, the probe `ordo doctor` runs after the host's apply (ordo/render/open_webui_probe.py)
        runs in it once, and a failing verdict, or a probe that could not run, is one entry.
        """
        if not self.broker:
            return self._error(503, "no container backend: this control plane cannot read or recreate containers")
        try:
            state = self.broker.backend.stack_state()
            doc = self.broker.backend.rendered_compose()
        except Exception as e:  # noqa: BLE001 - any unreadable side means the changed set is unknown
            return self._error(503, f"cannot read what is rendered and what is running ({e}); "
                                    "nothing was recreated")
        changes = diff_services(state.rendered, state.running, compose_version=state.compose_version)
        jobs = stale_one_shot_jobs(state.rendered, state.running, compose_version=state.compose_version)
        removed_jobs = [job.service for job in jobs if job.removable]
        host_reasons = self._host_reasons(changes, doc, self._render(), state.rendered)
        to_recreate = [change.service for change in changes if change.service not in host_reasons]
        _args, targets = plan_named(doc, to_recreate, force_recreate=True)
        # Every service the render no longer defines that is not already stopped is stopped (the host's `ordo apply`
        # does the same); one already stopped is left as an orphan.
        unrendered = set(state.running) - set(state.rendered)
        stopped = sorted(name for name in unrendered if state.running[name].state not in STOPPED_STATES)
        host = sorted(host_reasons)
        plan: dict[str, Any] = {
            "dry_run": dry_run,
            "changes": [{"service": change.service, "reasons": list(change.reasons)} for change in changes],
            "recreated": sorted(targets),
            "stopped": stopped,
            "removed_jobs": removed_jobs,
            "running_jobs": [job.service for job in jobs if not job.removable],
            "restart_required_on_host": host,
            "host_reasons": host_reasons,
            "host_command": f"ordo apply --only {' '.join(host)}" if host else None,
            # Containers the render no longer defines that were already stopped.
            "orphans": sorted(unrendered - set(stopped)),
            "warnings": [],
        }
        conflict = self.lease.group_conflict(sorted(targets))
        if conflict:
            return {**conflict, "changes": plan["changes"]}
        if dry_run:
            return {"ok": True, **plan}
        try:
            for name in stopped:
                self.broker.backend.stop(name)
            if to_recreate:
                self.broker.backend.recreate_services(to_recreate)
            if removed_jobs:
                self.broker.backend.remove_stopped_containers(removed_jobs)
        except Exception as e:  # noqa: BLE001 - reported with the plan it was executing
            return self._error(500, f"applying the render failed: {e}", changes=plan["changes"])
        if OPEN_WEBUI_SERVICE in targets:
            ok, line = self._open_webui_verdict()
            if not ok:
                plan["warnings"].append(line)
        return {"ok": True, **plan}

    def _open_webui_verdict(self) -> tuple[bool, str]:
        """(ok, one-line report) from the probe run inside the open-webui container, once, with no
        wait or retry (as `ordo doctor` runs it). A probe that cannot run is a failed verdict."""
        try:
            exit_code, output = self.broker.backend.exec_in_service(OPEN_WEBUI_SERVICE,
                                                                 ["python", "-c", OPEN_WEBUI_PROBE])
        except Exception as e:  # noqa: BLE001 - the container is already recreated; this only reports
            return False, f"! open-webui: could not verify its model-gateway connection ({type(e).__name__}: {e})"
        lines = output.strip().splitlines()
        if exit_code != 0:
            detail = lines[-1] if lines else f"exit {exit_code}"
            return False, f"! open-webui: could not verify its model-gateway connection (probe failed: {detail})"
        try:
            report = json.loads(lines[0]) if lines else None
        except ValueError:
            report = None
        if not isinstance(report, dict):
            return False, (f"! open-webui: could not verify its model-gateway connection "
                           f"(unreadable probe output: {output.strip()[:200]})")
        return open_webui_verdict(report)

    def apply(self, body: dict[str, Any]) -> dict[str, Any]:
        """`POST /apply`: bring the stack to out/ as it is rendered now (after a host render, say).
        `{"dry_run": true}` returns the plan and changes nothing; otherwise `confirm` is required."""
        dry_run = bool(body.get("dry_run"))
        if not dry_run and not confirmed(body):
            return self._error(400, 'Destructive operation requires confirmation. Set {"confirm": true} in the '
                                    'request body to proceed, or {"dry_run": true} for the plan.')
        return self.apply_render(dry_run=dry_run)

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

    def _leased_gpu_uuid(self) -> str | None:
        """The uuid of the card the scheduler leases (the primary card), or None when unknown."""
        try:
            gpu = self._render().hardware.primary_gpu
        except Exception:  # noqa: BLE001 - unknown means managed.gpu_refusal fails closed
            return None
        return getattr(gpu, "uuid", None) or None

    @staticmethod
    def _gpu_indexes() -> dict[str, str]:
        """nvidia-smi index -> uuid for every card on the host (ordo/render/gpu_live.py), so a device
        named by index resolves to one card. Empty when unreadable: an index then proves nothing."""
        try:
            return {str(card["index"]): str(card["uuid"]) for card in gpu_live.live_gpus()
                    if card.get("uuid") and card.get("index") is not None}
        except Exception:  # noqa: BLE001 - unknown means managed.gpu_refusal fails closed on indexes
            return {}

    def managed_projects_overview(self) -> dict[str, Any]:
        return self.managed_projects.overview()

    def managed_project_containers(self, project: str) -> dict[str, Any]:
        return self.managed_projects.containers(project)

    def managed_container_logs(self, project: str, name: str, query: dict[str, str]) -> dict[str, Any]:
        return self.managed_projects.container_logs(project, name, query)

    def managed_container_restart(self, project: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.managed_projects.container_restart(project, name, body, leased_gpu_uuid=self._leased_gpu_uuid,
                                                       gpu_indexes=self._gpu_indexes)

    # --- Registry routes ---
    # Derived from the render on every call (ordo/render/served_models.py): which models the stack
    # serves, from which file, on which GPU. There is no stored registry to drift from ordo.yaml.

    def _served_models(self) -> dict[str, dict[str, Any]]:
        rc = self._render()
        return served_models(rc.compose_dict(), rc.env, rc.gpu_inventory())

    def registry_models(self) -> dict[str, Any]:
        """Every model the current render serves, keyed by model id."""
        return {"models": self._served_models()}

    def live_gpus(self) -> dict[str, Any]:
        """Every card's live VRAM, utilization and temperature (ordo/render/gpu_live.py)."""
        return {"gpus": gpu_live.live_gpus()}

    def registry_gpus(self) -> dict[str, Any]:
        """Live GPU info (gpu_live) with the models the render pins to each card."""
        live = self._live_gpus()
        uuid_to_models = models_by_gpu(self._served_models())
        result: dict[str, Any] = {}
        for uuid, info in live.items():
            result[uuid] = {**info, "models": uuid_to_models.get(uuid, [])}
        return {"gpus": result}

    # --- ComfyUI model downloads (ordo/control/comfyui.py) ---

    def models_download(self, body: dict[str, Any]) -> dict[str, Any]:
        """Start a resumable file download to the ComfyUI models directory."""
        return self.downloads.start(body, worker=self._run_model_download)

    def models_download_status(self) -> dict[str, Any]:
        return self.downloads.status()

    def _run_model_download(self, url: str, category: str, filename: str) -> None:
        self.downloads.run(url, category, filename)

    # --- Slice 3: diagnostics routes ---

    def diagnostics_dstate(self) -> dict[str, Any]:
        """Report uninterruptible-sleep (D-state) processes across running containers."""
        wedged = []
        scanned = 0
        errors = []
        try:
            proc = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=30,
            )
            container_names = [n.strip() for n in proc.stdout.splitlines() if n.strip()]
            for name in container_names:
                scanned += 1
                try:
                    top = subprocess.run(
                        ["docker", "top", name, "-eo", "pid,stat,wchan:40,comm"],
                        capture_output=True, text=True, timeout=10,
                    )
                    lines = top.stdout.strip().splitlines()
                    if len(lines) < 2:
                        continue
                    # Parse header to find column indices
                    header = lines[0].split()
                    try:
                        pid_idx = header.index("PID")
                        stat_idx = header.index("STAT")
                        wchan_idx = header.index("WCHAN")
                        comm_idx = header.index("COMMAND")
                    except ValueError:
                        errors.append(f"{name}: unexpected ps columns {header}")
                        continue
                    for line in lines[1:]:
                        parts = line.split()
                        if len(parts) < len(header):
                            continue
                        stat = parts[stat_idx]
                        if not stat.startswith("D"):
                            continue
                        wchan = parts[wchan_idx]
                        wedged.append({
                            "container": name,
                            "pid": parts[pid_idx],
                            "stat": stat,
                            "wchan": wchan,
                            "comm": parts[comm_idx],
                            "p9": "p9" in wchan,
                        })
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
        except Exception as exc:
            errors.append(f"docker ps failed: {exc}")
        return {
            "scanned": scanned,
            "wedged": wedged,
            "p9_wedged": [w for w in wedged if w["p9"]],
            "errors": errors,
        }

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

    def gpu_assign_gone(self, target: str = "") -> dict[str, Any]:
        """410 GONE. GPU pins are baked at `ordo render` time, not at runtime.

        The v1 flow wrote overrides/gpu-assignments.yml and recreated the service. Under the render
        substrate nothing reads that file back and a recreate replays the already-rendered compose
        byte for byte, so the endpoint answered {"ok": true} while changing nothing. This mirrors
        the /guardian/* retirement: an honest 410 beats a silent no-op.
        """
        return self._error(
            410,
            "GPU reassignment moved to the render pipeline: set the service's `gpu_pin:` in its "
            "manifest and re-render (`ordo render`), then recreate the service. "
            "Runtime reassignment was a silent no-op and has been retired.",
        )

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

    def _live_gpus(self) -> dict[str, dict[str, Any]]:
        """The live GPU reader's cards keyed by uuid, in the GiB units /registry/gpus has always used."""
        out: dict[str, dict[str, Any]] = {}
        for card in gpu_live.live_gpus():
            used_mib = card["vram_used_mib"]
            out[card["uuid"]] = {
                "name": card["name"],
                "total_gb": round(card["vram_total_mib"] / 1024.0, 1),
                "used_gb": round(used_mib / 1024.0, 1) if used_mib is not None else None,
                "util": card["utilization_pct"],
            }
        return out

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
    def jobs_history(self) -> dict[str, Any]:
        """Finished leases, newest first: what the orchestration tab's history table shows."""
        return {"history": self.history.tail(100) if self.history else []}

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
