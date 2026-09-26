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

import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import yaml

from ..render import gpu_live, substrate
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
from .audit import AuditLog
from .broker import SELF_REFERENTIAL_SERVICES, Broker
from .scheduler import Job, Scheduler

logger = logging.getLogger(__name__)

# The only paths reachable without the bearer token: container healthchecks carry no credentials.
UNAUTHENTICATED_PATHS = frozenset({"/health", "/healthz"})

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

# Model download validation (matches ops-api)
COMFYUI_CATEGORIES = (
    "checkpoints", "loras", "text_encoders", "latent_upscale_models",
    "vae", "unet", "clip", "clip_vision", "controlnet", "embeddings",
    "upscale_models", "diffusion_models", "vae_approx",
)

_MODEL_DOWNLOAD_ALLOWED_HOSTS = {
    "huggingface.co", "hf-mirror.com", "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.huggingface.co", "cdn-lfs-eu-1.huggingface.co",
    "civitai.com", "github.com", "objects.githubusercontent.com",
}

COMFYUI_MODELS_DIR = Path(os.environ.get("COMFYUI_MODELS_DIR", "/models/comfyui"))
AUDIT_LOG_PATH = Path(os.environ.get("AUDIT_LOG_PATH", "/data/audit.jsonl"))
COMFYUI_CUSTOM_NODES_DIR = Path(os.environ.get("COMFYUI_CUSTOM_NODES_DIR", "/comfyui-app/ComfyUI/custom_nodes"))
COMFYUI_CONTAINER_NAME = os.environ.get("COMFYUI_CONTAINER_NAME", "ordo-comfyui-1")
# One path segment of a ComfyUI custom-node pack. Deliberately narrower than the filesystem
# allows: the segment is interpolated into a container path that a pip invocation then reads.
_NODE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,64}")

# --- audit ---
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


def _clip(value: Any, limit: int = _AUDIT_FIELD_MAX) -> str:
    return str(value)[:limit]


def audit_actor(header: str | None) -> str:
    """The caller's self-declared name from `X-Actor`, made safe to log; 'unknown' when absent."""
    actor = _ACTOR_UNSAFE.sub("", (header or "").strip())[:_ACTOR_MAX]
    return actor or "unknown"


def audit_subject(path: str, body: Any) -> tuple[str, str]:
    """(action, target) for a state-changing call. Reads only the one body field that names the
    target, so nothing else a caller sends (a URL's query string, a credential) reaches the log."""
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


def _validate_download_url(url: str) -> None:
    """Block SSRF: only allow HTTPS to known model-hosting domains, reject private IPs."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        raise ValueError("URL must use HTTPS on the standard port")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("Cannot parse hostname from URL")
    if host not in _MODEL_DOWNLOAD_ALLOWED_HOSTS:
        raise ValueError(
            f"Host {host!r} not in allowed list. "
            f"Allowed: {', '.join(sorted(_MODEL_DOWNLOAD_ALLOWED_HOSTS))}"
        )
    try:
        for info in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM):
            addr = ipaddress.ip_address(info[4][0])
            if addr.is_private or addr.is_reserved or addr.is_loopback or addr.is_link_local:
                raise ValueError(f"Host {host!r} resolves to private/reserved IP {addr}")
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host {host!r}: {exc}") from exc


def _validated_redirect_url(current_url: str, location: str) -> str:
    redirect_url = urljoin(current_url, location)
    _validate_download_url(redirect_url)
    return redirect_url


def _auto_detect_category(url: str, filename: str) -> str:
    """Auto-detect ComfyUI model category from URL/filename."""
    url_lower = url.lower()
    name_lower = filename.lower()
    for cat in sorted(COMFYUI_CATEGORIES, key=len, reverse=True):
        if cat in url_lower or cat in name_lower:
            return cat
    combined = f"{url_lower} {name_lower}"
    for keyword, category in (
        ("lora", "loras"),
        ("text_encoder", "text_encoders"),
        ("clip", "text_encoders"),
        ("vae", "vae"),
        ("unet", "unet"),
        ("controlnet", "controlnet"),
        ("upscale", "upscale_models"),
        ("embedding", "embeddings"),
    ):
        if keyword in combined:
            return category
    return "checkpoints"


class LifecycleGroupUnknown(Exception):
    """The rendered compose could not be read, so a service's netns members are unknown."""


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
    ):
        self.source_path = Path(source_path)
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
        # Slice 3: model download/pull state (in-process, not persisted)
        self._dl_lock = threading.Lock()
        self._dl_status = {"running": False, "output": "", "done": True, "success": None, "progress": 0, "filename": "", "category": ""}
        # The audit sink: `handle()` writes one record per state-changing call. Built on first use
        # from AUDIT_LOG_PATH (read then, so tests can point it elsewhere); it creates its
        # directory on the first write only.
        self._audit_log: AuditLog | None = None


    # --- core operations (pure, testable) ---
    def _render(self) -> Any:
        return render(Source.load(self.source_path), self.catalog, self.registry)

    def _substrate_conflict(self) -> dict[str, Any] | None:
        """A 409 payload when out/ was last rendered from different inputs than this process ships.

        Rendering over it would silently revert whatever the newer side changed (the image renders
        from its own baked copy of ordo/, catalog/ and the manifests). No manifest, or one written
        before renders recorded a digest, is allowed: this render then records ours.
        """
        manifest_path = self.out_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            recorded = json.loads(manifest_path.read_text(encoding="utf-8")).get("substrate_digest")
        except (OSError, ValueError, AttributeError) as e:
            return self._error(409, f"cannot read {manifest_path} to check the render substrate ({e}); "
                               "re-render from the host checkout, then retry")
        if not recorded or recorded == self.substrate_digest:
            return None
        return self._error(
            409,
            f"ops-controller's render substrate ({self.substrate_digest[:12]}) differs from the last "
            f"host render's ({str(recorded)[:12]}): the image is older or newer than the checkout that "
            "rendered out/, and a render here would silently change what that checkout rendered. "
            "Rebuild ordo/ops-controller from the checkout that rendered out/ (`ordo build "
            "ops-controller`), re-render, then `ordo recreate ops-controller`.",
            substrate_digest=self.substrate_digest, rendered_substrate_digest=recorded)

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
        self.source_path.write_text(text, encoding="utf-8")
        rendered.write(self.out_dir)
        if not self.broker:
            return None, None
        # A service the new render no longer defines (a disabled plugin's) is stopped by the apply.
        applied = self.apply_render()
        if "_status" not in applied:
            return applied, None
        self.source_path.write_text(previous_text, encoding="utf-8")
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
        conflict = self._group_lease_conflict(sorted(targets))
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
        if not dry_run and not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the "
                                    "request body to proceed, or {\"dry_run\": true} for the plan.")
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

    # --- Service lifecycle routes (ported from ops-api) ---

    # The lifecycle verbs live in the same process as the GPU scheduler, so they ask it before
    # starting anything. Starting an evicted resident during a lease (directly, by container
    # name, or through a whole-stack compose up) puts two tenants on one card: the 2026-08-08
    # host crash. Refusing here, once, replaces a "not during a lease" check in every caller.
    # Stop is never refused, and a lease tenant (comfyui) may still be cycled by its own gate.

    def _lease_conflict(self, service: str | None) -> dict[str, Any] | None:
        """A 409 payload when starting `service` (None = the whole stack) would share the card."""
        if not self.scheduler:
            return None
        status = self.scheduler.status()
        holders = [job["id"] for job in status["running"]] + [job["id"] for job in status["queued"]]
        evicted = status["evicted_residents"]
        if service is None and status["leased"]:
            return self._error(409, f"a GPU lease is active (held by {holders}, evicted {sorted(evicted)}); "
                                    "a whole-stack start would restart the evicted residents beside it. "
                                    "Name a service, or retry once the lease is released.",
                               lease_holders=holders)
        if service is not None and service in evicted:
            return self._error(409, f"{service!r} is evicted for a GPU lease held by {holders}; starting it "
                                    "would put two tenants on one card. The scheduler restores it when "
                                    "the lease is released.", lease_holders=holders)
        return None

    def _container_lease_conflict(self, container: str) -> dict[str, Any] | None:
        """`_lease_conflict` for a raw container name (`<project>-<service>-<n>`)."""
        if not self.scheduler:
            return None
        for service in self.scheduler.evicted_residents:
            if re.fullmatch(rf"[\w.-]+-{re.escape(service)}-\d+", container):
                return self._lease_conflict(service)
        return None

    # A service's netns members (`network_mode: service:<it>`) share its network namespace, so
    # every verb that gives it a new namespace must cycle them too, after it; otherwise they keep
    # running in the dead one with only `lo` (observed 2026-09-24: a caddy restart from the
    # dashboard cut off hermes-dashboard and every tailnet sidecar). The group comes from
    # `stack.lifecycle_group`, the planner the host's `ordo up` / `ordo recreate` use.

    def _rendered_compose(self, target: str) -> dict:
        try:
            return self.broker.backend.rendered_compose()
        except Exception as e:
            raise LifecycleGroupUnknown(
                f"cannot read the rendered compose to find {target!r}'s netns members ({e}); "
                "refusing rather than orphaning them") from e

    def _lifecycle_group(self, service: str) -> list[str]:
        """[service, *its netns members]. Raises LifecycleGroupUnknown when the render is unreadable."""
        return lifecycle_group(self._rendered_compose(service), service)

    def _container_members(self, container: str) -> list[str]:
        """The netns members that follow a raw container (`<project>-<service>-<n>`), or []."""
        doc = self._rendered_compose(container)
        # Longest name first, so `ordo-tailnet-chat-1` is tailnet-chat even if a `chat` exists.
        for service in sorted(doc.get("services") or {}, key=len, reverse=True):
            if re.fullmatch(rf"[\w.-]+-{re.escape(service)}-\d+", container):
                return lifecycle_group(doc, service)[1:]
        return []

    def _group_lease_conflict(self, group: list[str]) -> dict[str, Any] | None:
        """`_lease_conflict` for each service a verb would start."""
        for service in group:
            conflict = self._lease_conflict(service)
            if conflict:
                return conflict
        return None

    def _restart_members(self, members: list[str]) -> None:
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
    def _with_members(payload: dict[str, Any], members: list[str]) -> dict[str, Any]:
        if members:
            payload["members"] = members
        return payload

    def service_start(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if body.get("dry_run"):
            return {"would": "start", "service": service_id}
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        try:
            group = self._lifecycle_group(service_id)
        except LifecycleGroupUnknown as e:
            return self._error(500, str(e))
        conflict = self._group_lease_conflict(group)
        if conflict:
            return conflict
        try:
            self.broker.backend.start(service_id)
            self._restart_members(group[1:])
        except Exception as e:
            return self._error(500, str(e))
        return self._with_members({"ok": True, "service": service_id, "action": "started"}, group[1:])

    def service_stop(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if body.get("dry_run"):
            return {"would": "stop", "service": service_id}
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        try:
            group = self._lifecycle_group(service_id)
        except LifecycleGroupUnknown as e:
            return self._error(500, str(e))
        try:
            # Members first: stopping only the owner leaves them running in a dead namespace.
            for member in group[1:]:
                self.broker.backend.stop(member)
            self.broker.backend.stop(service_id)
        except Exception as e:
            return self._error(500, str(e))
        return self._with_members({"ok": True, "service": service_id, "action": "stopped"}, group[1:])

    def service_restart(self, service_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if body.get("dry_run"):
            return {"would": "restart", "service": service_id}
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        try:
            group = self._lifecycle_group(service_id)
        except LifecycleGroupUnknown as e:
            return self._error(500, str(e))
        conflict = self._group_lease_conflict(group)
        if conflict:
            return conflict
        try:
            self.broker.backend.restart(service_id)
            self._restart_members(group[1:])
        except Exception as e:
            return self._error(500, str(e))
        return self._with_members({"ok": True, "service": service_id, "action": "restarted"}, group[1:])

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
        # list_containers below. Wrapping it again here produced
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
            group = self._lifecycle_group(service_id)
        except LifecycleGroupUnknown as e:
            return self._error(500, str(e))
        conflict = self._group_lease_conflict(group)
        if conflict:
            return conflict
        try:
            # One compose call recreates the whole group: the backend plans it with the same
            # `stack.plan_named` the host's `ordo recreate` uses.
            self.broker.backend.recreate_service(service_id)
        except Exception as e:
            return self._error(500, str(e))
        return self._with_members({"ok": True, "service": service_id, "action": "recreated"}, group[1:])

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
            members = self._container_members(name)
        except LifecycleGroupUnknown as e:
            return self._error(500, str(e))
        conflict = self._container_lease_conflict(name) or self._group_lease_conflict(members)
        if conflict:
            return conflict
        try:
            self.broker.backend.container_restart(name)
            self._restart_members(members)
        except Exception as e:
            return self._error(500, str(e))
        return self._with_members({"ok": True, "container": name, "action": "restarted"}, members)

    def service_stats(self) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        try:
            stats = self.broker.backend.service_stats()
        except Exception as e:
            return self._error(500, str(e))
        return stats

    def compose_up(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        service = body.get("service") or None
        try:
            # A named compose verb acts on the service's whole lifecycle group (the backend
            # expands it through `bringup`), so every member is lease-checked too.
            conflict = (self._group_lease_conflict(self._lifecycle_group(service)) if service
                        else self._lease_conflict(None))
        except LifecycleGroupUnknown as e:
            return self._error(500, str(e))
        if conflict:
            return conflict
        try:
            self.broker.backend.compose_up(service)
        except Exception as e:
            return self._error(500, str(e))
        return {"ok": True, "action": "compose-up"}

    def compose_down(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        service = body.get("service") or None
        try:
            self.broker.backend.compose_down(service)
        except Exception as e:
            return self._error(500, str(e))
        return {"ok": True, "action": "compose-down"}

    def compose_restart(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.broker:
            return self._error(503, "no broker configured")
        if not body.get("confirm"):
            return self._error(400, "Destructive operation requires confirmation. Set {\"confirm\": true} in the request body to proceed.")
        service = body.get("service") or None
        try:
            # A named compose verb acts on the service's whole lifecycle group (the backend
            # expands it through `bringup`), so every member is lease-checked too.
            conflict = (self._group_lease_conflict(self._lifecycle_group(service)) if service
                        else self._lease_conflict(None))
        except LifecycleGroupUnknown as e:
            return self._error(500, str(e))
        if conflict:
            return conflict
        try:
            self.broker.backend.compose_restart(service)
        except Exception as e:
            return self._error(500, str(e))
        return {"ok": True, "action": "compose-restart"}

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

    # --- Slice 3: model download/pull routes ---

    def models_download(self, body: dict[str, Any]) -> dict[str, Any]:
        """Start a resumable file download to the ComfyUI models directory."""
        url = str(body.get("url", "")).strip()
        if not url.startswith("https://"):
            return self._error(400, "URL must start with https://")
        try:
            _validate_download_url(url)
        except ValueError as e:
            return self._error(400, str(e))
        with self._dl_lock:
            if self._dl_status.get("running"):
                return self._error(409, "A download is already in progress")
        filename = str(body.get("filename", "")).strip() or url.split("/")[-1].split("?")[0]
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            return self._error(400, "Invalid or undetectable filename")
        category = str(body.get("category", "")).strip()
        if category and category not in COMFYUI_CATEGORIES:
            return self._error(400, f"Invalid category. Must be one of: {COMFYUI_CATEGORIES}")
        if not category:
            category = _auto_detect_category(url, filename)
        # Start download in background thread
        thread = threading.Thread(
            target=self._run_model_download,
            args=(url, category, filename),
            daemon=True,
        )
        thread.start()
        return {"status": "started", "category": category, "filename": filename}

    def models_download_status(self) -> dict[str, Any]:
        """Poll active download progress."""
        with self._dl_lock:
            return dict(self._dl_status)

    def _run_model_download(self, url: str, category: str, filename: str) -> None:
        """Background download worker."""
        with self._dl_lock:
            self._dl_status.update({
                "running": True, "output": f"Starting: {filename}", "done": False,
                "success": None, "progress": 0, "filename": filename, "category": category,
            })
        dest_dir = COMFYUI_MODELS_DIR / category
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            with self._dl_lock:
                self._dl_status.update({
                    "output": f"Cannot create dir: {e}", "success": False,
                    "running": False, "done": True,
                })
            return

        dest = dest_dir / filename
        temp_path = dest.with_suffix(dest.suffix + ".tmp")
        try:
            import httpx
            start_byte = temp_path.stat().st_size if temp_path.exists() else 0
            req_headers = {"User-Agent": "ordo-ai-stack/1.0"}
            if start_byte > 0:
                req_headers["Range"] = f"bytes={start_byte}-"
            with httpx.Client(timeout=60.0, follow_redirects=False) as client:
                current_url = url
                for _ in range(10):
                    _validate_download_url(current_url)
                    response = client.send(
                        client.build_request("GET", current_url, headers=req_headers),
                        stream=True,
                    )
                    if response.status_code not in (301, 302, 303, 307, 308):
                        break
                    location = response.headers.get("location")
                    response.close()
                    if not location:
                        raise ValueError("Redirect response did not include a location")
                    current_url = _validated_redirect_url(current_url, location)
                else:
                    raise ValueError("Too many redirects while downloading model")
                with response:
                    r = response
                    r.raise_for_status()
                    total = 0
                    total_header = r.headers.get("Content-Range") or r.headers.get("Content-Length")
                    if total_header and "/" in str(total_header):
                        total = int(str(total_header).split("/")[-1].strip())
                    elif r.headers.get("Content-Length"):
                        total = int(r.headers["Content-Length"]) + (start_byte or 0)
                    total_mb = total / (1024 * 1024) if total else 0
                    downloaded = start_byte
                    append = start_byte > 0 and r.status_code == 206
                    with open(temp_path, "ab" if append else "wb") as f:
                        for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                            f.write(chunk)
                            downloaded += len(chunk)
                            dl_mb = downloaded / (1024 * 1024)
                            pct = int(downloaded * 100 / total) if total else 0
                            msg = f"Downloading {filename} → {category}/\n"
                            msg += f"{dl_mb:.0f} / {total_mb:.0f} MB ({pct}%)" if total else f"{dl_mb:.0f} MB downloaded"
                            with self._dl_lock:
                                self._dl_status["output"] = msg
                                self._dl_status["progress"] = pct
            temp_path.rename(dest)
            with self._dl_lock:
                self._dl_status["success"] = True
                self._dl_status["output"] += f"\nDone — saved to {category}/{filename}"
        except Exception as e:
            with self._dl_lock:
                self._dl_status["output"] += f"\nError: {e}"
                self._dl_status["success"] = False
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
        finally:
            with self._dl_lock:
                self._dl_status["running"] = False
                self._dl_status["done"] = True

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

    def _audit_sink(self) -> AuditLog:
        if self._audit_log is None:
            self._audit_log = AuditLog(AUDIT_LOG_PATH)
        return self._audit_log

    def audit_call(
        self,
        method: str,
        path: str,
        body: Any,
        actor: str,
        status: int,
        error: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Write the one record for a state-changing call.

        The record holds only named fields (see `audit_subject`): never the request body, the
        headers or a credential. Never raises: an audit failure must not fail the action, but it
        is logged so a broken log does not go unnoticed.
        """
        fields = body if isinstance(body, dict) else {}
        action, target = audit_subject(path, body)
        extra: dict[str, Any] = {
            "method": method.upper(),
            "path": _clip(path),
            "status": status,
            "dry_run": bool(fields.get("dry_run")),
            "confirm": bool(fields.get("confirm")),
        }
        if error:
            extra["error"] = _clip(error, _AUDIT_ERROR_MAX)
        if detail:
            extra["detail"] = _clip(detail)
        try:
            self._audit_sink().record(action=action, target=target, result=audit_result(status),
                                      caller=actor, **extra)
        except Exception:
            logger.exception("ops-controller could not write the audit record for %s %s", method, path)

    def _lease_detail(self, path: str, body: Any, status: int, payload: Any) -> str | None:
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

    def handle(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        query: dict[str, str] | None,
        actor: str,
    ) -> tuple[int, dict]:
        """`route()` plus the audit record: the HTTP binding's one entry point.

        Every call with an AUDITED_METHODS method leaves exactly one record, whatever route() does
        with it (success, dry run, 4xx refusal, 5xx failure or an exception). Reads leave none.
        """
        if method.upper() not in AUDITED_METHODS:
            return self.route(method, path, body, query)
        try:
            status, payload = self.route(method, path, body, query)
        except Exception as e:
            self.audit_call(method, path, body, actor, 500, str(e) or type(e).__name__)
            raise
        error = payload.get("error") if isinstance(payload, dict) else None
        self.audit_call(method, path, body, actor, status, str(error) if error else None,
                        self._lease_detail(path, body, status, payload))
        return status, payload

    @staticmethod
    def _validate_custom_node_path(node_path: str) -> str | None:
        """Relative path under ComfyUI custom_nodes. Returns None if it is not one."""
        cleaned = (node_path or "").strip().strip("/").replace("\\", "/")
        if not cleaned or len(cleaned) > 240 or ".." in cleaned:
            return None
        for segment in cleaned.split("/"):
            if not segment or not _NODE_PATH_SEGMENT.fullmatch(segment):
                return None
        return cleaned

    def comfyui_install_node_requirements(self, body: dict[str, Any] | None) -> dict[str, Any]:
        """pip install -r a custom node pack's requirements INSIDE the running comfyui container.

        Installing on the host would put the packages somewhere ComfyUI never imports from.
        """
        body = body or {}
        if not body.get("confirm"):
            return self._error(
                400,
                "Destructive operation requires confirmation. "
                'Set {"confirm": true} in the request body to proceed.',
            )
        node_path = self._validate_custom_node_path(body.get("node_path") or "")
        if node_path is None:
            return self._error(400, "Invalid node_path")
        requirements_on_host = COMFYUI_CUSTOM_NODES_DIR / node_path / "requirements.txt"
        if not requirements_on_host.is_file():
            return self._error(
                404, f"No requirements.txt at custom_nodes/{node_path}/requirements.txt"
            )
        if not self.broker:
            return self._error(503, "no container backend")
        requirements_in_container = f"/root/ComfyUI/custom_nodes/{node_path}/requirements.txt"
        try:
            exit_code, output = self.broker.backend.exec_in(
                COMFYUI_CONTAINER_NAME,
                ["python3", "-m", "pip", "install", "-r", requirements_in_container],
            )
        except FileNotFoundError:
            return self._error(
                503, f"Container {COMFYUI_CONTAINER_NAME!r} not found - start comfyui first"
            )
        except Exception as exc:
            return self._error(500, f"exec failed: {exc}")
        if len(output) > 12000:
            output = output[:12000] + "\n... [truncated]"
        ok = exit_code == 0
        result: dict[str, Any] = {
            "ok": ok,
            "exit_code": exit_code,
            "output": output,
            "node_path": node_path,
        }
        if not ok:
            result["_status"] = 500
            result["error"] = f"pip install exited {exit_code}"
        return result

    def gpu_assign_gone(self, target: str = "") -> dict[str, Any]:
        """410 GONE. GPU pins are baked at `ordo render` time, not at runtime.

        The v1 flow wrote overrides/gpu-assignments.yml and recreated the service. Under the render
        substrate nothing reads that file back and a recreate replays the already-rendered compose
        byte for byte, so the endpoint answered {"ok": true} while changing nothing. This mirrors
        the /guardian/* retirement: an honest 410 beats a silent no-op.
        """
        return self._error(
            410,
            "GPU reassignment moved to the render pipeline: set the pin in ordo.yaml "
            "(overrides:) and re-render (`ordo render`), then recreate the service. "
            "Runtime reassignment was a silent no-op and has been retired.",
        )

    def audit_log(self, limit: int = 50) -> dict[str, Any]:
        """The newest `limit` audit records, newest first, across the rotated generations."""
        try:
            return {"entries": self._audit_sink().tail(limit)}
        except OSError as e:
            return {"entries": [], "error": f"failed to read audit log: {e}"}

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

    # --- routing (also pure) ---
    def route(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        body = body or {}
        query = query or {}
        m = method.upper()
        if m == "GET" and path == "/status":
            return 200, self.status()
        if m == "GET" and path == "/model-config":
            return 200, self.get_model_config()
        if m == "POST" and path == "/model-config":
            return self._as_response(self.set_model_config(body))
        if m == "POST" and path == "/apply":
            return self._as_response(self.apply(body))
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
        if m == "GET" and path in ("/health", "/healthz"):
            # The digest is not secret; `ordo doctor` reads it here to compare with the checkout.
            return 200, {"ok": True, "substrate_digest": self.substrate_digest}
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
        if m == "POST" and path == "/compose/up":
            return self._as_response(self.compose_up(body))
        if m == "POST" and path == "/compose/down":
            return self._as_response(self.compose_down(body))
        if m == "POST" and path == "/compose/restart":
            return self._as_response(self.compose_restart(body))
        # Registry routes (ported from ops-api, slice 2)
        if m == "GET" and path == "/registry/models":
            return 200, self.registry_models()
        if m == "GET" and path == "/registry/gpus":
            return 200, self.registry_gpus()
        if m == "GET" and path == "/gpus":
            return 200, self.live_gpus()
        # Slice 3: model download/pull routes
        if m == "POST" and path == "/models/download":
            return self._as_response(self.models_download(body))
        if m == "GET" and path == "/models/download/status":
            return 200, self.models_download_status()
        # Slice 3: diagnostics routes
        if m == "GET" and path == "/diagnostics/dstate":
            return 200, self.diagnostics_dstate()
        # Slice 3: audit route
        if m == "GET" and path == "/audit":
            try:
                limit = int(query.get("limit", "50"))
            except ValueError:
                return 422, {"error": "limit must be an integer"}
            if not 1 <= limit <= AUDIT_READ_LIMIT_MAX:
                return 422, {"error": f"limit must be between 1 and {AUDIT_READ_LIMIT_MAX}"}
            return 200, self.audit_log(limit)
        # Slice 4: ComfyUI node requirements, plus the two honest 410s
        if m == "POST" and path == "/comfyui/install-node-requirements":
            return self._as_response(self.comfyui_install_node_requirements(body))
        if m == "POST" and path == "/gpu/assign":
            return self._as_response(self.gpu_assign_gone((body or {}).get("service", "")))
        if m == "POST" and path.startswith("/registry/models/") and path.endswith("/assign-gpu"):
            return self._as_response(self.gpu_assign_gone(path.split("/")[3]))
        return 404, {"error": f"no route {method} {path}"}

    @staticmethod
    def _error(status: int, message: str, **extra: Any) -> dict[str, Any]:
        return {"_status": status, "error": message, **extra}

    @staticmethod
    def _as_response(payload: dict[str, Any]) -> tuple[int, dict]:
        status = int(payload.pop("_status", 200)) if isinstance(payload, dict) else 200
        return status, payload

    def app(self, auth_token: str | Callable[[], str] | None):
        """Build the FastAPI application that delegates every authenticated request to route().

        `auth_token` is required: without one the API would be open to every container on the
        network, so an empty token is refused here rather than silently served. It may be a function
        that reads the token (serve passes one reading the /run/secrets file): each request then
        checks against the current value, so a rotated file takes effect without a restart. A read
        that fails or comes back empty (a torn write) keeps the last good token, never an open API.
        """
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse

        read_token = auth_token if callable(auth_token) else (lambda: auth_token or "")
        current = {"token": read_token().strip()}
        if not current["token"]:
            raise ValueError("ops-controller needs OPS_CONTROLLER_TOKEN: refusing to serve an unauthenticated API")

        def _expected() -> bytes:
            try:
                token = read_token().strip()
            except Exception:  # noqa: BLE001 - an unreadable file keeps the last good token
                token = ""
            if token:
                current["token"] = token
            return f"bearer {current['token']}".encode()

        cp = self
        app = FastAPI(title="ops-controller")

        def _authorized(header: str) -> bool:
            # Normalise only the scheme's case; the token itself is compared exactly, in constant time.
            scheme, _, token = header.partition(" ")
            presented = f"{scheme.lower()} {token.strip()}".encode()
            return hmac.compare_digest(presented, _expected())

        @app.middleware("http")
        async def dispatch(request: Request, call_next):
            method = request.method
            path = request.url.path
            actor = audit_actor(request.headers.get(ACTOR_HEADER))
            audited = method.upper() in AUDITED_METHODS
            if path not in UNAUTHENTICATED_PATHS and not _authorized(request.headers.get("authorization", "")):
                client = request.client.host if request.client else "unknown"
                # Never log the presented credential, right or wrong.
                logger.warning("ops-controller refused %s %s from %s: missing or invalid bearer token (401)",
                               method, path, client)
                error = "missing or invalid bearer token"
                if audited:
                    # The body of an unauthenticated call is never read, so the record has only
                    # what the method and path say.
                    cp.audit_call(method, path, None, actor, 401, error)
                return JSONResponse(content={"error": error}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            body = None
            if method in ("POST", "PUT", "PATCH"):
                try:
                    raw = await request.body()
                    if raw:
                        body = json.loads(raw)
                except json.JSONDecodeError:
                    error = "invalid JSON body"
                    cp.audit_call(method, path, None, actor, 400, error)
                    return JSONResponse(content={"error": error}, status_code=400)
            status, payload = cp.handle(method, path, body, dict(request.query_params), actor)
            return JSONResponse(content=payload, status_code=status)

        return app

    def serve(self, auth_token: str | Callable[[], str] | None, host: str = "0.0.0.0",
              port: int = 9000) -> None:  # pragma: no cover - needs a socket
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

        app = self.app(auth_token)
        uvicorn.run(app, host=host, port=port, log_level="warning")
