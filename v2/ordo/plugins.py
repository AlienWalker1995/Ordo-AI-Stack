"""Plugin registry — plugins declare their hardware needs + config fragment (data, not code).

The renderer reads manifests and composes enabled plugins into the rendered config. A plugin
is enabled only if it's requested (auto/explicit), its hardware requirements are met, AND its
declared dependencies are also enabled. Media plugins self-declare NVIDIA-only.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

from .hardware import HardwareProfile


@dataclasses.dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    description: str
    nvidia: bool
    vram_gb: float
    ram_gb: float
    depends_on: tuple[str, ...]
    provides: tuple[str, ...]
    compose_profile: str
    env: dict[str, str]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Plugin":
        req = d.get("requires", {}) or {}
        return cls(
            id=str(d["id"]), name=str(d.get("name", d["id"])),
            description=str(d.get("description", "")),
            nvidia=bool(req.get("nvidia", False)),
            vram_gb=float(req.get("vram_gb", 0)), ram_gb=float(req.get("ram_gb", 0)),
            depends_on=tuple(d.get("depends_on", []) or []),
            provides=tuple(d.get("provides", []) or []),
            compose_profile=str(d.get("compose_profile", "")),
            env={str(k): str(v) for k, v in (d.get("env", {}) or {}).items()},
        )

    def fits(self, hw: HardwareProfile) -> bool:
        if self.nvidia and not hw.has_gpu:
            return False
        if self.vram_gb and hw.primary_vram_gb < self.vram_gb:
            return False
        if self.ram_gb and hw.ram_gb and hw.ram_gb < self.ram_gb:
            return False
        return True


class PluginRegistry:
    def __init__(self, plugins: list[Plugin]):
        self.plugins = plugins
        self._by_id = {p.id: p for p in plugins}

    @classmethod
    def load(cls, plugins_dir: str | Path) -> "PluginRegistry":
        base = Path(plugins_dir)
        found: list[Plugin] = []
        if base.is_dir():
            for manifest in sorted(base.glob("*/plugin.yaml")):
                data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
                found.append(Plugin.from_dict(data))
        return cls(found)

    def get(self, plugin_id: str) -> Plugin | None:
        return self._by_id.get(plugin_id)

    def resolve(
        self, requested: Any, hw: HardwareProfile,
    ) -> tuple[list[Plugin], list[str]]:
        """Return (enabled plugins, notes). 'auto' = everything the hardware can run."""
        notes: list[str] = []
        if requested == "auto" or requested is None:
            wanted = {p.id for p in self.plugins}
        else:
            wanted = set(requested)
            for pid in wanted - set(self._by_id):
                notes.append(f"requested plugin '{pid}' is not in the registry (ignored)")

        # hardware gate
        enabled: dict[str, Plugin] = {}
        for pid in wanted:
            p = self._by_id.get(pid)
            if not p:
                continue
            if p.fits(hw):
                enabled[pid] = p
            elif requested != "auto":
                notes.append(f"'{pid}' needs {'NVIDIA + ' if p.nvidia else ''}"
                             f"{p.vram_gb:.0f}GB VRAM — not available; skipped")

        # dependency gate: drop plugins whose deps aren't all enabled (iterate to fixpoint)
        changed = True
        while changed:
            changed = False
            for pid in list(enabled):
                for dep in enabled[pid].depends_on:
                    if dep not in enabled:
                        notes.append(f"'{pid}' disabled — dependency '{dep}' not enabled")
                        del enabled[pid]
                        changed = True
                        break

        ordered = [p for p in self.plugins if p.id in enabled]
        return ordered, notes
