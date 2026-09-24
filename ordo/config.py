"""Load + validate the declarative source (ordo.yaml) — the single source of truth."""
from __future__ import annotations

import dataclasses
import difflib
import re
from pathlib import Path
from typing import Any

import yaml

from .hardware import GPU, HardwareProfile

# `site:` flows verbatim into the rendered .env, so each key must be a usable env var name.
_SITE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Options that used to exist: a leftover key gets a migration hint instead of a typo suggestion.
_RETIRED_KEYS = {
    "cloud_fallback": "was removed (it routed oversized GPU jobs to a route nothing polled); "
                      "delete the block",
}


def _reject_unknown_keys(where: str, keys: Any, valid: list[str]) -> None:
    """Raise ValueError naming the first key not in `valid`, with the closest valid key as a hint."""
    for key in keys:
        if key in valid:
            continue
        if where == "ordo.yaml" and key in _RETIRED_KEYS:
            raise ValueError(f"{where}: key {key!r} {_RETIRED_KEYS[key]}")
        close = difflib.get_close_matches(str(key), valid, n=1)
        hint = f"did you mean {close[0]!r}?" if close else f"valid keys: {valid}"
        raise ValueError(f"{where}: unknown key {key!r} ({hint})")


@dataclasses.dataclass
class Source:
    hardware: Any = "auto"          # "auto" or an explicit hardware spec dict
    tier: str = "auto"
    model: str = "auto"
    agent: str = "hermes"
    # Control-plane UI (pluggable, like the agent): any `services/<id>/dashboard.yaml`.
    dashboard: str = "dashboard"
    plugins: Any = "auto"           # "auto" or list[str]
    overrides: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Host/site config — NOT derived from the model and NOT secret: bind-mount roots
    # (DATA_PATH, BASE_PATH, CODE_ROOT), the edge hostnames (CADDY_*), and any other
    # operator-environment key the plugin manifests interpolate (e.g. COMFYUI_IMAGE,
    # N8N_WEBHOOK_URL). These flow verbatim into the rendered `.env` so `${DATA_PATH}` /
    # `${BASE_PATH}` etc. resolve deterministically instead of defaulting to `./data`
    # relative to out/. Kept in the source (single source of truth) so there is no
    # hand-edit of a derived output and no drift.
    site: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Electricity-derived per-token cost for the local models (usd_per_kwh, inference_watts,
    # prompt_tokens_per_second, output_tokens_per_second). Optional: empty means $0/token
    # (the historical default). See render.local_token_costs for the formula.
    cost: dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Source:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        _reject_unknown_keys("ordo.yaml", data, [f.name for f in dataclasses.fields(cls)])
        s = cls(
            hardware=data.get("hardware", "auto"),
            tier=str(data.get("tier", "auto")),
            model=str(data.get("model", "auto")),
            agent=str(data.get("agent", "hermes")),
            dashboard=str(data.get("dashboard", "dashboard")),
            plugins=data.get("plugins", "auto"),
            overrides=data.get("overrides") or {},
            site=data.get("site") or {},
            cost=data.get("cost") or {},
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
        for key in self.site:
            if not isinstance(key, str) or not _SITE_KEY.match(key):
                raise ValueError(f"site: key {key!r} is not an env var name (expected {_SITE_KEY.pattern})")
        if isinstance(self.hardware, dict):
            _reject_unknown_keys("hardware", self.hardware, [f.name for f in dataclasses.fields(HardwareProfile)])
            for i, gpu in enumerate(self.hardware.get("gpus") or []):
                if isinstance(gpu, dict):
                    _reject_unknown_keys(f"hardware.gpus[{i}]", gpu, [f.name for f in dataclasses.fields(GPU)])
        if not isinstance(self.cost, dict):
            raise ValueError("cost must be a mapping")
        if self.plugins != "auto" and not isinstance(self.plugins, list):
            raise ValueError("plugins must be 'auto' or a list")
