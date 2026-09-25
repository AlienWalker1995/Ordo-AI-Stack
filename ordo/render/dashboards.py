"""Dashboard registry — the control-plane UI is pluggable (like agents), declared as data.

The Ordo "dashboard" is the operator's control-plane web UI, and it is pluggable the same way
agents are: drop a `services/<id>/dashboard.yaml` in and set `dashboard: <id>` in ordo.yaml, and
the substrate needs no patching. The shipped one (`dashboard`) talks straight to the `ordo serve`
control plane (service `ops-controller`).

A dashboard manifest declares:
  - `image`  ("" -> the <project>/dashboard convention), and
  - `environment` / `depends_on` / `healthcheck` for the dashboard service.

A dashboard has no backend service of its own: `ops-controller` is the control plane, and a
dashboard's frontend is same-origin (`/api/*`) with its server reading `OPS_CONTROLLER_URL` at
runtime. (A companion `backend:` service used to be declarable here, for the transitional
`ops-api` while the routes were ported. Nothing declares one now.)
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from .buildspec import BuildSpec
from .plugins import EdgeSite, LocalPort, parse_derived_env
from .secret_files import SecretFileRef, parse_secret_files


def _gpu_caps(b: dict[str, Any]) -> tuple[str, ...]:
    """Parse a dashboard's GPU reservation capabilities, data-driven. Accepts either the explicit
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
class Dashboard:
    id: str
    name: str
    description: str
    image: str                       # "" -> the <project>/dashboard convention
    default: bool
    environment: dict[str, str] = dataclasses.field(default_factory=dict)
    volumes: tuple[str, ...] = ()    # on-disk model dirs etc. (${VAR} refs pass through)
    depends_on: dict[str, str] = dataclasses.field(default_factory=dict)
    healthcheck: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Secret NAMES the dashboard reads, rendered as `KEY: ${KEY}` (see PluginService.secrets).
    secrets: tuple[str, ...] = ()
    # Secrets it reads from a file under /run/secrets (see PluginService.secret_files).
    secret_files: tuple[SecretFileRef, ...] = ()
    # Derived-config NAMES (keys of the rendered .env) the dashboard reads, rendered as
    # `KEY: ${KEY?...}` (see PluginService.derived_env). The dashboard never loads the whole .env.
    derived_env: tuple[str, ...] = ()
    # GPU visibility for the dashboard service. `hardware_stats()` shells to nvidia-smi (_probe_gpu)
    # and enumerates cards via gpu_stats.list_gpus for the hw-stat bar's GPU widgets — the NVIDIA
    # runtime only injects nvidia-smi/NVML when the service reserves a GPU with the `utility` cap.
    # Without it `hardware_stats()` returns gpu:null + gpus:[]. Declared via `gpu: utility` (or
    # `gpu_capabilities: [utility]`); `count: all` (empty device_ids) so it reads BOTH cards.
    gpu_capabilities: tuple[str, ...] = ()
    # Build-context identity (METADATA; NEVER rendered). Absent -> the dashboard's own
    # `services/<id>/`. The shipped dashboard's Dockerfile is nested (services/dashboard/app), so
    # it declares an explicit `build.context`. See ordo/render/buildspec.py.
    build: BuildSpec = dataclasses.field(default_factory=BuildSpec)
    # Loopback host port while the edge is off (see plugins.LocalPort). None -> no local access.
    local_port: LocalPort | None = None
    # The secret the local operator signs in with while the edge is off (no SSO identity exists).
    # Passed to the dashboard, and required in secrets.env, only while `local_port` is published.
    # Always delivered as a file: the dashboard reads `<KEY>_FILE` (/run/secrets/<key lowercased>).
    local_login_secret: str = ""
    # The dashboard's own SSO-gated edge port and upstream (see plugins.EdgeSite). None -> none.
    edge_site: EdgeSite | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Dashboard:
        where = f"dashboard {d.get('id')!r}"
        if "wants_secrets" in d:
            raise ValueError(
                f"{where}: `wants_secrets` was replaced by `secrets: [NAMES]`. A service now lists the "
                "secret names it reads, instead of receiving the whole secrets.env")
        local_port = LocalPort.from_manifest(d.get("local_port"), where)
        local_login_secret = str(d.get("local_login_secret", "") or "")
        if local_login_secret and local_port is None:
            raise ValueError(f"{where}: `local_login_secret` needs a `local_port`: it is the sign-in for that port")
        secrets = tuple(str(k) for k in (d.get("secrets", []) or []))
        return cls(
            id=str(d["id"]), name=str(d.get("name", d["id"])),
            description=str(d.get("description", "")),
            image=str(d.get("image", "")),
            default=bool(d.get("default", False)),
            environment={str(k): str(v) for k, v in (d.get("environment", {}) or {}).items()},
            volumes=tuple(str(v) for v in (d.get("volumes", []) or [])),
            depends_on={str(k): str(v) for k, v in (d.get("depends_on", {}) or {}).items()},
            healthcheck=dict(d.get("healthcheck", {}) or {}),
            secrets=secrets,
            secret_files=parse_secret_files(where, d.get("secret_files"), env_secrets=secrets,
                                            explicit_env=d.get("environment") or {}),
            derived_env=parse_derived_env(where, d),
            gpu_capabilities=_gpu_caps(d),
            build=BuildSpec.from_dict(d.get("build")),
            local_port=local_port,
            local_login_secret=local_login_secret,
            edge_site=EdgeSite.from_manifest(d.get("edge_site"), where),
        )

    def image_for(self, project: str) -> str:
        return self.image or f"{project}/dashboard"


class DashboardRegistry:
    def __init__(self, dashboards: list[Dashboard]):
        self.dashboards = dashboards
        self._by_id = {d.id: d for d in dashboards}

    @classmethod
    def load(cls, dashboards_dir: str | Path) -> DashboardRegistry:
        import yaml
        # Co-located manifests: each dashboard declares itself in `services/<id>/dashboard.yaml`.
        # sorted() over the glob keys the registry by path (== by folder id) so order is stable.
        base = Path(dashboards_dir)
        dashboards = [
            Dashboard.from_dict(yaml.safe_load(manifest.read_text(encoding="utf-8")) or {})
            for manifest in sorted(base.glob("*/dashboard.yaml"))
        ]
        return cls(dashboards)

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
