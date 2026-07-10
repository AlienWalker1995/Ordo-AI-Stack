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
class PluginService:
    """One compose service a kind=service plugin declares — data, not code. compose.py
    renders these directly instead of hardcoding per-plugin if-blocks."""
    name: str
    image: str
    gpu: bool = False               # request a GPU reservation for this service
    gpu_pin: str = ""               # ""|"secondary": pin to a specific card via CUDA_VISIBLE_DEVICES
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    command: list[str] = dataclasses.field(default_factory=list)
    volumes: list[str] = dataclasses.field(default_factory=list)
    healthcheck: dict[str, Any] = dataclasses.field(default_factory=dict)
    depends_on: list[str] = dataclasses.field(default_factory=list)
    # True → this service reads the operator-managed `secrets.env` as a second env_file (so its
    # ${SECRET} refs resolve). Secret VALUES never live in the rendered config, only the reference.
    wants_secrets: bool = False
    # Host port publishes. RESERVED for the edge/front-door plugin (Caddy's :443) — core services
    # deliberately publish none (isolation). Opt-in behind the plugin's profile, so it stays dormant
    # until `--profile edge` unless the edge plugin is enabled.
    ports: list[str] = dataclasses.field(default_factory=list)
    # /dev/shm size (compose `shm_size`, e.g. "1gb"). Docker defaults to 64MB, which starves
    # Electron/Chromium + Selkies-style streaming GUIs (frame buffers live in shared memory) and
    # drops the session mid-stream. Empty → omit the key (docker default). Data-driven like gpu.
    shm_size: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PluginService":
        return cls(
            name=str(d["name"]), image=str(d["image"]),
            gpu=bool(d.get("gpu", False)), gpu_pin=str(d.get("gpu_pin", "")),
            env={str(k): str(v) for k, v in (d.get("env", {}) or {}).items()},
            command=[str(c) for c in (d.get("command", []) or [])],
            volumes=[str(v) for v in (d.get("volumes", []) or [])],
            healthcheck=dict(d.get("healthcheck", {}) or {}),
            depends_on=[str(x) for x in (d.get("depends_on", []) or [])],
            wants_secrets=bool(d.get("wants_secrets", False)),
            ports=[str(p) for p in (d.get("ports", []) or [])],
            shm_size=str(d.get("shm_size", "")),
        )


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
    kind: str = "service"          # "service" (compose service) | "mcp" (agent tool server)
    mcp: dict[str, Any] = dataclasses.field(default_factory=dict)  # image/env/tools for kind=mcp
    services: tuple[PluginService, ...] = ()  # compose services this plugin contributes (kind=service)
    # secret env KEYS this plugin's services need at runtime (names only, values operator-managed).
    # render emits these into secrets.env.example; the rendered compose reads them via a second
    # env_file `secrets.env` — derived (.env) config and operator secrets stay in separate files.
    secrets: tuple[str, ...] = ()

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
            kind=str(d.get("kind", "service")),
            mcp=dict(d.get("mcp", {}) or {}),
            services=tuple(PluginService.from_dict(s) for s in (d.get("services", []) or [])),
            secrets=tuple(str(s) for s in (d.get("secrets", []) or [])),
        )

    @property
    def needs_secondary_gpu(self) -> bool:
        """True if ANY of this plugin's services must be pinned to a non-primary GPU
        (its image has no kernels for the primary card, so primary-fallback would ship a crash)."""
        return any(s.gpu_pin == "secondary" for s in self.services)

    def fits(self, hw: HardwareProfile) -> bool:
        if self.nvidia and not hw.has_gpu:
            return False
        if self.vram_gb and hw.primary_vram_gb < self.vram_gb:
            return False
        if self.ram_gb and hw.ram_gb and hw.ram_gb < self.ram_gb:
            return False
        # A secondary-pinned plugin (voice) needs an actual second card — never fall back to the
        # primary, because these images CRASH there (Pascal-only kernels).
        if self.needs_secondary_gpu and hw.secondary_gpu is None:
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
            elif p.needs_secondary_gpu and hw.has_gpu and hw.secondary_gpu is None:
                # Always warn (even under 'auto'): this is the Pascal-1070 pin — falling back to
                # the primary 5090 would ship a guaranteed crash, so we gate OFF instead.
                notes.append(f"'{pid}' needs a SECONDARY GPU (its images have no kernels for the "
                             "primary card) — only one GPU detected; disabled")
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
