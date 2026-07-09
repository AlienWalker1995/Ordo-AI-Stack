"""Dashboard registry — the control-plane UI is pluggable (like agents), declared as data.

The Ordo "dashboard" is the operator's control-plane web UI. The V2 substrate SHIPS a minimal,
V2-native single-file SPA (`v2-native`, the open-source default) that talks straight to the
`ordo serve` control plane. But a deployment can select a different dashboard — e.g. this
operator's feature-rich V1 dashboard (`v1-parity`: GGUF model management via /api/llm/*,
model-control flag cards, GPU/model-registry views, Grafana tab, token auth) — WITHOUT patching
the substrate. Selection is data-driven, mirroring the agent registry: drop a
`dashboards/<id>/dashboard.yaml` in and set `dashboard: <id>` in ordo.yaml.

A dashboard manifest declares:
  - `image`  ("" -> the <project>/dashboard:latest convention),
  - `environment` / `depends_on` / `healthcheck` for the dashboard service, and
  - an OPTIONAL `backend:` service (its own image/env/volumes/depends/healthcheck). The V1
    dashboard's frontend is same-origin (`/api/*`) and its FastAPI backend reads the backend
    URL from `OPS_CONTROLLER_URL` at runtime, so the V1 image is reused UNCHANGED and pointed at
    a dedicated backend service (`ops-api`) — this keeps V2's `ordo serve` scheduler service
    named `ops-controller` (its live clients depend on that name) with ZERO collision.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any


def _gpu_caps(b: dict[str, Any]) -> tuple[str, ...]:
    """Parse a backend's GPU reservation capabilities, data-driven. Accepts either the explicit
    `gpu_capabilities: [utility]` list OR the `gpu: <cap>` shorthand string (e.g. `gpu: utility`).
    Empty/absent -> no reservation. Mirrors the shorthand-or-list style used elsewhere in the
    manifests so a service just declares what visibility it needs."""
    caps = b.get("gpu_capabilities")
    if caps:
        return tuple(str(c) for c in caps)
    gpu = b.get("gpu")
    if isinstance(gpu, str) and gpu:
        return (gpu,)
    if isinstance(gpu, list) and gpu:
        return tuple(str(c) for c in gpu)
    return ()


@dataclasses.dataclass(frozen=True)
class DashboardBackend:
    """An OPTIONAL companion backend service a dashboard needs (e.g. the V1 ops-controller API,
    rendered as service `ops-api`). Data-driven — compose renders it verbatim alongside the
    dashboard service. Empty name -> the dashboard has no separate backend."""
    name: str = ""
    image: str = ""                  # "" -> the <project>/<name>:latest convention
    environment: dict[str, str] = dataclasses.field(default_factory=dict)
    volumes: tuple[str, ...] = ()
    depends_on: dict[str, str] = dataclasses.field(default_factory=dict)
    healthcheck: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Add the backend to the root group (0) for Docker-socket access on Docker Desktop
    # (root:root socket) — mirrors V1's ops-controller `group_add: ["0"]`.
    group_add_root: bool = False
    wants_secrets: bool = True       # reads secrets.env (OPS_CONTROLLER_TOKEN etc.) as a 2nd env_file
    # GPU visibility for the backend, as capabilities on an all-GPU reservation. The V1-parity
    # `ops-api` backend enumerates GPUs/VRAM by shelling to nvidia-smi (it IS a copy of V1's
    # ops-controller), which the NVIDIA runtime only injects when the service reserves a GPU with
    # the `utility` capability — so this backend MUST declare `gpu: utility` (or the equivalent
    # `gpu_capabilities: [utility]`) or it enumerates ZERO GPUs and the dashboard's GPU widgets
    # report "No GPUs returned from registry". `count: all` (via the empty device_ids) so it reads
    # BOTH cards. Empty -> no reservation (a backend that doesn't touch the GPU). Mirrors V1 exactly.
    gpu_capabilities: tuple[str, ...] = ()

    def image_for(self, project: str) -> str:
        return self.image or f"{project}/{self.name}:latest"


@dataclasses.dataclass(frozen=True)
class Dashboard:
    id: str
    name: str
    description: str
    image: str                       # "" -> the <project>/dashboard:latest convention
    default: bool
    environment: dict[str, str] = dataclasses.field(default_factory=dict)
    volumes: tuple[str, ...] = ()    # on-disk model dirs etc. (${VAR} refs pass through)
    depends_on: dict[str, str] = dataclasses.field(default_factory=dict)
    healthcheck: dict[str, Any] = dataclasses.field(default_factory=dict)
    wants_secrets: bool = True
    backend: DashboardBackend | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Dashboard":
        b = d.get("backend") or None
        backend = None
        if b:
            backend = DashboardBackend(
                name=str(b.get("name", "")),
                image=str(b.get("image", "")),
                environment={str(k): str(v) for k, v in (b.get("environment", {}) or {}).items()},
                volumes=tuple(str(v) for v in (b.get("volumes", []) or [])),
                depends_on={str(k): str(v) for k, v in (b.get("depends_on", {}) or {}).items()},
                healthcheck=dict(b.get("healthcheck", {}) or {}),
                group_add_root=bool(b.get("group_add_root", False)),
                wants_secrets=bool(b.get("wants_secrets", True)),
                gpu_capabilities=_gpu_caps(b),
            )
        return cls(
            id=str(d["id"]), name=str(d.get("name", d["id"])),
            description=str(d.get("description", "")),
            image=str(d.get("image", "")),
            default=bool(d.get("default", False)),
            environment={str(k): str(v) for k, v in (d.get("environment", {}) or {}).items()},
            volumes=tuple(str(v) for v in (d.get("volumes", []) or [])),
            depends_on={str(k): str(v) for k, v in (d.get("depends_on", {}) or {}).items()},
            healthcheck=dict(d.get("healthcheck", {}) or {}),
            wants_secrets=bool(d.get("wants_secrets", True)),
            backend=backend,
        )

    def image_for(self, project: str) -> str:
        return self.image or f"{project}/dashboard:latest"


class DashboardRegistry:
    def __init__(self, dashboards: list[Dashboard]):
        self.dashboards = dashboards
        self._by_id = {d.id: d for d in dashboards}

    @classmethod
    def load(cls, dashboards_dir: str | Path) -> "DashboardRegistry":
        base = Path(dashboards_dir)
        found: list[Dashboard] = []
        if base.is_dir():
            for manifest in sorted(base.glob("*/dashboard.yaml")):
                import yaml
                data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
                found.append(Dashboard.from_dict(data))
        return cls(found)

    def get(self, dashboard_id: str) -> Dashboard | None:
        return self._by_id.get(dashboard_id)

    def default_dashboard(self) -> Dashboard | None:
        for d in self.dashboards:
            if d.default:
                return d
        return self.dashboards[0] if self.dashboards else None

    def resolve(self, dashboard_id: str) -> tuple[Dashboard | None, list[str]]:
        """Resolve the chosen dashboard. Unknown id -> a note + fall back to the default (so a
        typo surfaces at render/preflight rather than as a mystery at compose-up)."""
        notes: list[str] = []
        d = self._by_id.get(dashboard_id)
        if d is None:
            avail = ", ".join(sorted(self._by_id)) or "(none registered)"
            notes.append(f"dashboard '{dashboard_id}' is not in the registry (available: {avail})")
            return self.default_dashboard(), notes
        return d, notes
