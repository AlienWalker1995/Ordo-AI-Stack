"""Control plane — the ops-controller service, as pure request handlers.

This is what the rendered compose's `ops-controller` service runs. It exposes the substrate
over HTTP: the live GPU/scheduler status the dashboard and agents poll, a re-render endpoint,
and the drift-safe model switch.

Design constraints (from the architecture decisions + the drift lessons):
  - ONE write path. Changing the active model does NOT hand-edit `.env` or a separate registry;
    it writes the *declarative source* (`ordo.yaml`) and re-renders. `.env` is always a pure
    function of the source, so a runtime model switch can never drift the three ctx values apart.
  - The handlers are pure (method, path, body) -> (status, dict) so they're testable with no
    server/socket. `serve()` is a thin FastAPI binding around `route()` (no-cover).
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
from .gpu_assignments_fmt import parse_gpu_assignments_yaml
from .model_registry import ModelRegistry
from .plugins import PluginRegistry
from .render import render
from .scheduler import Job, Scheduler
from .source_edit import edit_plugins_list

# Service plugins Hermes may install/enable on request (kind=service, profile-gated). The core
# substrate (llamacpp, litellm-db, model-gateway, model-gateway-keys, ops-controller, dashboard,
# agent), the edge / front-door (edge, tailnet-names — secret-dependent, host `make up` only), and
# the agent itself are NOT here, so they can never be created/removed via this path — the
# allowlist is the security gate.
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
        registry_path: str | Path | None = None,
    ):
        self.source_path = Path(source_path)
        self.catalog = catalog
        self.registry = registry
        self.out_dir = Path(out_dir)
        self.scheduler = scheduler
        self.broker = broker
        self.history = history  # LeaseHistory sink (shared with the broker) — /jobs/history
        # Model registry (runtime state) — same store as ops-api reads.
        # registry_path defaults to /data/model-registry.json (mounted in compose).
        if registry_path is None:
            import os
            registry_path = os.environ.get("MODEL_REGISTRY_PATH", "/data/model-registry.json")
        self.model_registry = ModelRegistry(
            registry_path=Path(registry_path),
            env_path=Path("/config/.env"),
            gpu_assignments_path=Path("/config/overrides/gpu-assignments.yml"),
        )

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
        enabled = set(rc.plugins_enabled)
        present = self._secrets_present()
        return {"plugins": [
            self._plugin_view(p, enabled, present, rc.hardware)
            for p in self.registry.plugins if p.id in INSTALLABLE_PLUGINS
        ]}

    def enable_plugin(self, plugin_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Enable a service plugin the drift-safe way (same one-write-path as set_model_config): add
        it (+ any unmet deps) to ordo.yaml's `plugins:` list, re-render, regenerate out/. Under
        `plugins: auto` a fitting plugin is ALREADY rendered (dormant behind its profile), so this is
        a no-op edit and the caller just recreates the service. Refuses anything not in
        INSTALLABLE_PLUGINS, and anything that doesn't fit the hardware."""
        if plugin_id not in INSTALLABLE_PLUGINS:
            return self._error(403, f"'{plugin_id}' is not an installable service (core, edge/"
                               "front-door, and the agent are refused)",
                               installable=sorted(INSTALLABLE_PLUGINS))
        plugin = self.registry.get(plugin_id)
        if plugin is None:
            return self._error(404, f"plugin '{plugin_id}' is not in the registry")
        src = Source.load(self.source_path)
        rc = self._render()
        hw = rc.hardware
        services = [s.name for s in plugin.services]
        present = self._secrets_present()

        if plugin_id in set(rc.plugins_enabled):
            # already rendered (the common case under plugins: auto) — no source edit; recreate only
            return {"ok": True, "already_rendered": True, "plugin": plugin_id,
                    "services": services, "compose_profile": plugin.compose_profile,
                    "wants_secrets": bool(plugin.secrets),
                    "missing_secrets": [k for k in plugin.secrets if k not in present],
                    "warnings": []}

        if not plugin.fits(hw):
            _, notes = self.registry.resolve([plugin_id], hw)
            reason = next((n for n in notes if plugin_id in n),
                          f"'{plugin_id}' does not fit this hardware")
            return self._error(409, reason)

        if src.plugins == "auto" or src.plugins is None:
            # fits + auto but not enabled -> a dependency was gated off (dropped by the dep fixpoint)
            _, notes = self.registry.resolve([plugin_id], hw)
            reason = next((n for n in notes if plugin_id in n),
                          f"'{plugin_id}' could not be enabled (an unmet dependency)")
            return self._error(409, reason)

        # explicit plugin list: add the plugin + any unmet deps, VALIDATE the render, then persist.
        to_add = self._deps_closure(plugin_id, set(rc.plugins_enabled))
        blocked = [pid for pid in to_add if pid not in INSTALLABLE_PLUGINS]
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
        if plugin_id not in set(rc2.plugins_enabled):
            return self._error(409, f"'{plugin_id}' still not enabled after the edit (unmet "
                               "dependency or fit) — nothing written")
        # commit: ONE write path — the source text, then regenerate every derived output.
        self.source_path.write_text(text, encoding="utf-8")
        rc2.write(self.out_dir)
        return {"ok": True, "already_rendered": False, "plugin": plugin_id,
                "services": services, "compose_profile": plugin.compose_profile,
                "wants_secrets": bool(plugin.secrets),
                "missing_secrets": [k for k in plugin.secrets if k not in self._secrets_present()],
                "added": to_add, "warnings": rc2.warnings, "wrote": str(self.out_dir)}

    def disable_plugin(self, plugin_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Remove a service plugin from an EXPLICIT plugins list + re-render (symmetric to enable).
        Under `plugins: auto` there's no list item to remove — the caller stops the container, but it
        returns on the next render unless the operator sets an explicit list; that is reported."""
        if plugin_id not in INSTALLABLE_PLUGINS:
            return self._error(403, f"'{plugin_id}' is not an installable service")
        plugin = self.registry.get(plugin_id)
        if plugin is None:
            return self._error(404, f"plugin '{plugin_id}' is not in the registry")
        services = [s.name for s in plugin.services]
        src = Source.load(self.source_path)
        if src.plugins == "auto" or src.plugins is None:
            return {"ok": True, "transient": True, "plugin": plugin_id, "services": services,
                    "note": "plugins is 'auto'; the container is stopped but returns on the next "
                            "render — set an explicit plugin list to persist a disable"}
        text = self.source_path.read_text(encoding="utf-8")
        try:
            new_text = edit_plugins_list(text, plugin_id, "remove")
        except ValueError as e:
            return self._error(422, f"cannot safely edit ordo.yaml plugins list: {e}")
        if new_text == text:
            return {"ok": True, "already_absent": True, "plugin": plugin_id, "services": services}
        edited = Source.from_dict(yaml.safe_load(new_text))
        rc2 = render(edited, self.catalog, self.registry)
        self.source_path.write_text(new_text, encoding="utf-8")
        rc2.write(self.out_dir)
        return {"ok": True, "plugin": plugin_id, "services": services, "wrote": str(self.out_dir)}

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

    # --- Service lifecycle routes (ported from ops-api) ---

    def service_start(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if body.get("dry_run"):
            return {"would": "start", "service": service_id}
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        try:
            self.broker.backend.start(service_id)
        except Exception as e:
            return self._error(500, str(e))
        return {"ok": True, "service": service_id, "action": "started"}

    def service_stop(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if body.get("dry_run"):
            return {"would": "stop", "service": service_id}
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        try:
            self.broker.backend.stop(service_id)
        except Exception as e:
            return self._error(500, str(e))
        return {"ok": True, "service": service_id, "action": "stopped"}

    def service_restart(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if body.get("dry_run"):
            return {"would": "restart", "service": service_id}
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        try:
            self.broker.backend.restart(service_id)
        except Exception as e:
            return self._error(500, str(e))
        return {"ok": True, "service": service_id, "action": "restarted"}

    def service_logs(self, service_id: str, tail: int = 100) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        try:
            logs = self.broker.backend.logs(service_id, tail=tail)
        except Exception as e:
            return self._error(500, str(e))
        return {"logs": logs, "service": service_id}

    def list_services(self) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        try:
            services = self.broker.backend.list_services()
        except Exception as e:
            return self._error(500, str(e))
        # The backend returns the ops-api payload already ({"services": [...]}), the same as
        # list_containers and mcp_containers below. Wrapping it again here produced
        # {"services": {"services": [...]}}, which the dashboard would read as an empty grid.
        return services

    def service_recreate(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if body.get("dry_run"):
            return {"would": "recreate", "service": service_id}
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        try:
            self.broker.backend.recreate_service(service_id)
        except Exception as e:
            return self._error(500, str(e))
        return {"ok": True, "service": service_id, "action": "recreated"}

    def list_containers(self) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        try:
            containers = self.broker.backend.list_containers()
        except Exception as e:
            return self._error(500, str(e))
        return containers

    def container_logs(self, name: str, tail: int = 100) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        try:
            logs = self.broker.backend.container_logs(name, tail=tail)
        except Exception as e:
            return self._error(500, str(e))
        return logs

    def container_restart(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        try:
            self.broker.backend.container_restart(name)
        except Exception as e:
            return self._error(500, str(e))
        return {"ok": True, "container": name, "action": "restarted"}

    def service_stats(self) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        try:
            stats = self.broker.backend.service_stats()
        except Exception as e:
            return self._error(500, str(e))
        return stats

    def mcp_containers(self) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        try:
            containers = self.broker.backend.mcp_containers()
        except Exception as e:
            return self._error(500, str(e))
        return containers

    def compose_up(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        try:
            self.broker.backend.compose_up()
        except Exception as e:
            return self._error(500, str(e))
        return {"ok": True, "action": "compose-up"}

    def compose_down(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        try:
            self.broker.backend.compose_down()
        except Exception as e:
            return self._error(500, str(e))
        return {"ok": True, "action": "compose-down"}

    def compose_restart(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        try:
            self.broker.backend.compose_restart()
        except Exception as e:
            return self._error(500, str(e))
        return {"ok": True, "action": "compose-restart"}

    # --- Registry routes (ported from ops-api, slice 2) ---
    # These read the RUNTIME model registry (model-registry.json), not the static
    # catalog. The registry is the source of truth for which models are enabled,
    # on which GPU, with what config — exactly what ops-api serves.

    def registry_models(self) -> dict[str, Any]:
        """List all models in the runtime registry — same shape as ops-api's /registry/models."""
        models = self.model_registry.list_models()
        return {"models": {mid: rec.model_dump() for mid, rec in models.items()}}

    def registry_get_model(self, model_id: str) -> tuple[int, dict[str, Any]]:
        """Get a single model record by ID — same shape as ops-api's /registry/models/{id}."""
        rec = self.model_registry.get(model_id)
        if rec is None:
            return 404, {"error": f"Model {model_id!r} not found"}
        return 200, rec.model_dump()

    def registry_gpus(self) -> dict[str, Any]:
        """Live GPU info merged with registry model assignments — same shape as ops-api's /registry/gpus."""
        # Get live GPU info from nvidia-smi (same as ops-api's _live_gpus())
        live = self._live_gpus()
        models = self.model_registry.list_models()
        # Build uuid -> list of model ids
        uuid_to_models: dict[str, list[str]] = {}
        for mid, m in models.items():
            if m.gpu_uuid:
                uuid_to_models.setdefault(m.gpu_uuid, []).append(mid)
        result: dict[str, Any] = {}
        for uuid, info in live.items():
            result[uuid] = {**info, "models": uuid_to_models.get(uuid, [])}
        return {"gpus": result}

    def gpu_assignments(self) -> dict[str, Any]:
        """Current service->GPU-uuid pins — same shape as ops-api's /gpu/assignments (dict, not list)."""
        path = Path("/config/overrides/gpu-assignments.yml")
        if not path.exists():
            return {"assignments": {}}
        return {"assignments": parse_gpu_assignments_yaml(path.read_text(encoding="utf-8"))}

    def _live_gpus(self) -> dict[str, dict[str, Any]]:
        """Query nvidia-smi for live GPU info — same as ops-api's _live_gpus()."""
        import subprocess
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=gpu_uuid,name,memory.total,memory.used,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0 or not result.stdout.strip():
                return {}
            out: dict[str, dict[str, Any]] = {}
            for line in result.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 5:
                    continue
                uuid, name, total_mib, used_mib, util = parts[:5]
                try:
                    out[uuid] = {
                        "name": name,
                        "total_gb": round(float(total_mib) / 1024.0, 1),
                        "used_gb": round(float(used_mib) / 1024.0, 1),
                        "util": int(float(util)),
                    }
                except (ValueError, TypeError):
                    continue
            return out
        except Exception:
            return {}

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
        if m == "GET" and path == "/plugins":
            return 200, self.list_plugins()
        if m == "POST" and path.startswith("/plugins/") and path.endswith("/enable"):
            return self._as_response(self.enable_plugin(path[len("/plugins/"):-len("/enable")], body))
        if m == "POST" and path.startswith("/plugins/") and path.endswith("/disable"):
            return self._as_response(self.disable_plugin(path[len("/plugins/"):-len("/disable")], body))
        if m == "POST" and path == "/jobs":
            return self._as_response(self.request_job(body))
        if m == "POST" and path == "/jobs/complete":
            return self._as_response(self.complete_job(body))
        if m == "POST" and path == "/jobs/heartbeat":
            return self._as_response(self.heartbeat_job(body))
        if m == "GET" and path == "/jobs/history":
            # Finished leases, newest first — what the orchestration tab's history table shows.
            return 200, {"history": self.history.tail(100) if self.history else []}
        if m == "GET" and path == "/jobs/cloud-routed":
            # Return-and-DRAIN the jobs the scheduler routed to cloud fallback: each job is
            # handed out exactly once, to whichever agent polls this endpoint (audit P1-2 —
            # previously drain_cloud_routed() had no caller and routed jobs sat forever).
            return 200, {"cloud_routed": self.scheduler.drain_cloud_routed()}
        if m == "GET" and path in ("/health", "/healthz"):
            return 200, {"ok": True}
        # Service lifecycle routes (ported from ops-api)
        if m == "POST" and path.startswith("/services/") and path.endswith("/start"):
            service_id = path[len("/services/"):-len("/start")]
            return self._as_response(self.service_start(service_id, body))
        if m == "POST" and path.startswith("/services/") and path.endswith("/stop"):
            service_id = path[len("/services/"):-len("/stop")]
            return self._as_response(self.service_stop(service_id, body))
        if m == "POST" and path.startswith("/services/") and path.endswith("/restart"):
            service_id = path[len("/services/"):-len("/restart")]
            return self._as_response(self.service_restart(service_id, body))
        if m == "GET" and path.startswith("/services/") and path.endswith("/logs"):
            service_id = path[len("/services/"):-len("/logs")]
            return self._as_response(self.service_logs(service_id))
        if m == "GET" and path == "/services":
            return self._as_response(self.list_services())
        if m == "POST" and path.startswith("/services/") and path.endswith("/recreate"):
            service_id = path[len("/services/"):-len("/recreate")]
            return self._as_response(self.service_recreate(service_id, body))
        if m == "GET" and path == "/containers":
            return self._as_response(self.list_containers())
        if m == "GET" and path.startswith("/containers/") and path.endswith("/logs"):
            name = path[len("/containers/"):-len("/logs")]
            return self._as_response(self.container_logs(name))
        if m == "POST" and path.startswith("/containers/") and path.endswith("/restart"):
            name = path[len("/containers/"):-len("/restart")]
            return self._as_response(self.container_restart(name, body))
        if m == "GET" and path == "/stats/services":
            return self._as_response(self.service_stats())
        if m == "GET" and path == "/mcp/containers":
            return self._as_response(self.mcp_containers())
        if m == "POST" and path == "/compose/up":
            return self._as_response(self.compose_up(body))
        if m == "POST" and path == "/compose/down":
            return self._as_response(self.compose_down(body))
        if m == "POST" and path == "/compose/restart":
            return self._as_response(self.compose_restart(body))
        # Registry routes (ported from ops-api, slice 2)
        if m == "GET" and path == "/registry/models":
            return 200, self.registry_models()
        if m == "GET" and path.startswith("/registry/models/") and path.count("/") == 3:
            model_id = path.split("/")[3]
            return self.registry_get_model(model_id)
        if m == "POST" and path.startswith("/registry/models/") and path.endswith("/enable"):
            model_id = path[len("/registry/models/"):-len("/enable")]
            return self._as_response(self.registry_enable_model(model_id, body))
        if m == "GET" and path == "/registry/gpus":
            return 200, self.registry_gpus()
        if m == "GET" and path == "/gpu/assignments":
            return 200, self.gpu_assignments()
        return 404, {"error": f"no route {method} {path}"}

    @staticmethod
    def _error(status: int, message: str, **extra: Any) -> dict[str, Any]:
        return {"_status": status, "error": message, **extra}

    @staticmethod
    def _as_response(payload: dict[str, Any]) -> tuple[int, dict]:
        status = int(payload.pop("_status", 200)) if isinstance(payload, dict) else 200
        return status, payload

    def app(self):
        """Build the FastAPI application that delegates every request to route()."""
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse

        cp = self
        app = FastAPI(title="ops-controller")

        @app.middleware("http")
        async def dispatch(request: Request, call_next):
            method = request.method
            path = request.url.path
            body = None
            if method in ("POST", "PUT", "PATCH"):
                try:
                    raw = await request.body()
                    if raw:
                        body = json.loads(raw)
                except json.JSONDecodeError:
                    return JSONResponse(content={"error": "invalid JSON body"}, status_code=400)
            status, payload = cp.route(method, path, body)
            return JSONResponse(content=payload, status_code=status)

        return app

    def serve(self, host: str = "0.0.0.0", port: int = 9000) -> None:  # pragma: no cover - needs a socket
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

        app = self.app()
        uvicorn.run(app, host=host, port=port, log_level="warning")
