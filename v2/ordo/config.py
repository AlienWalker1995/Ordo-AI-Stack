"""Load + validate the declarative source (ordo.yaml) — the single source of truth."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml


@dataclasses.dataclass
class Source:
    hardware: Any = "auto"          # "auto" or an explicit hardware spec dict
    tier: str = "auto"
    model: str = "auto"
    agent: str = "hermes"
    plugins: Any = "auto"           # "auto" or list[str]
    cloud_fallback: dict[str, Any] = dataclasses.field(default_factory=lambda: {"enabled": False})
    overrides: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Host/site config — NOT derived from the model and NOT secret: bind-mount roots
    # (DATA_PATH, BASE_PATH, CODE_ROOT), the edge hostnames (CADDY_*), and any other
    # operator-environment key the plugin manifests interpolate (e.g. COMFYUI_IMAGE,
    # N8N_WEBHOOK_URL). These flow verbatim into the rendered `.env` so `${DATA_PATH}` /
    # `${BASE_PATH}` etc. resolve deterministically instead of defaulting to `./data`
    # relative to out/. Kept in the source (single source of truth) so there is no
    # hand-edit of a derived output and no drift.
    site: dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Source":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Source":
        s = cls(
            hardware=data.get("hardware", "auto"),
            tier=str(data.get("tier", "auto")),
            model=str(data.get("model", "auto")),
            agent=str(data.get("agent", "hermes")),
            plugins=data.get("plugins", "auto"),
            cloud_fallback=data.get("cloud_fallback") or {"enabled": False},
            overrides=data.get("overrides") or {},
            site=data.get("site") or {},
        )
        s.validate()
        return s

    def validate(self) -> None:
        valid_tiers = {"auto", "cpu", "low", "medium", "high", "ultra"}
        if self.tier not in valid_tiers:
            raise ValueError(f"tier must be one of {sorted(valid_tiers)}, got {self.tier!r}")
        if not isinstance(self.overrides, dict):
            raise ValueError("overrides must be a mapping")
        if not isinstance(self.site, dict):
            raise ValueError("site must be a mapping of env KEY -> value")
        if self.plugins != "auto" and not isinstance(self.plugins, list):
            raise ValueError("plugins must be 'auto' or a list")
