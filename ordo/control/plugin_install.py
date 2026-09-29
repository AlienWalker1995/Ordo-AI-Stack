"""Service and MCP plugins the control plane may enable or disable, through the source.

The render authority for Hermes-driven onboarding and the dashboard's MCP toggle: an enable or a
disable edits only the source's `plugins:` list (ordo/render/source_edit.py), validates the render,
and commits through the post-render step (ordo/control/apply.py). What may be touched at all is
`installable`: the allowlist below plus every kind=mcp plugin.
"""
from __future__ import annotations

from typing import Any

import yaml

from ..render.config import Source
from ..render.source_edit import edit_plugins_list
from .apply import RenderApply
from .responses import CONFIRM_REQUIRED, confirmed, error
from .source import StackSource, enabled_ids

# Service plugins Hermes may install/enable on request (kind=service, profile-gated). The core
# substrate (llamacpp, litellm-db, model-gateway, model-gateway-keys, ops-controller, dashboard,
# agent), the edge / front-door (edge, tailnet-names — secret-dependent, host `make up` only), and
# the agent itself are NOT here, so they can never be created/removed via this path — the
# allowlist is the security gate. Every kind=mcp plugin (an agent tool server) is installable as
# well, derived from its manifest kind (see `installable`): the dashboard's MCP toggle persists
# through this same write path.
INSTALLABLE_PLUGINS = frozenset({
    "comfyui", "song-gen", "voice", "rag", "open-webui", "monitoring",
    "automation", "searxng-web", "codebase-memory-ui", "obsidian-livesync", "llamacpp-cpu",
})


class PluginInstaller:
    """`GET /plugins`, `POST /plugins/{id}/enable` and `POST /plugins/{id}/disable`."""

    def __init__(self, source: StackSource, applier: RenderApply):
        self.source = source
        self.applier = applier

    def installable(self, plugin_id: str) -> bool:
        """The allowlisted service plugins plus every kind=mcp plugin in the registry."""
        if plugin_id in INSTALLABLE_PLUGINS:
            return True
        plugin = self.source.registry.get(plugin_id)
        return plugin is not None and plugin.kind == "mcp"

    def _installable_ids(self) -> list[str]:
        return sorted(p.id for p in self.source.registry.plugins if self.installable(p.id))

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
            for p in self.source.registry.plugins if self.installable(p.id)
        ]}

    def enable(self, plugin_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Enable a service plugin the drift-safe way (same one-write-path as a model switch): add
        it (+ any unmet deps) to ordo.yaml's `plugins:` list, re-render, regenerate out/, then apply
        (`RenderApply.apply_render`), which creates its services. Under `plugins: auto` a fitting plugin is
        ALREADY rendered, so nothing is written and the apply alone creates whatever of it is not
        running. A service whose secrets out/secrets.env lacks is rendered but left to the host.
        Refuses anything not installable (`installable`), and anything that doesn't fit the hardware."""
        if not self.installable(plugin_id):
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
        blocked = [pid for pid in to_add if not self.installable(pid)]
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

    def disable(self, plugin_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Remove a service plugin from an EXPLICIT plugins list, re-render, then apply (symmetric to
        enable): the plugin's services, which the render no longer defines, are stopped, and whatever
        the render changed for the rest (the gateway, for an MCP server) is recreated. Under
        `plugins: auto` there is no list item to remove, so a disable could not persist: it is
        refused with the fix (an explicit list) and nothing is stopped."""
        if not self.installable(plugin_id):
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
