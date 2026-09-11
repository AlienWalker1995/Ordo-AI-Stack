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

from .buildspec import BuildSpec
from .gpu import GpuArbitration
from .hardware import HardwareProfile


@dataclasses.dataclass(frozen=True)
class PluginService:
    """One compose service a kind=service plugin declares — data, not code. compose.py
    renders these directly instead of hardcoding per-plugin if-blocks."""
    name: str
    image: str
    gpu: bool = False               # request a GPU reservation for this service
    gpu_pin: str = ""               # ""|"secondary": pin to a specific card via CUDA_VISIBLE_DEVICES
    # How this service's GPU use is ARBITRATED (resident/lease/gated/exempt + its runtime VRAM
    # footprint and which card it contends for). `gpu`/`gpu_pin` above are compose WIRING — they
    # say the container gets a device; this says what the scheduler must do about it. REQUIRED
    # for any service that renders a GPU reservation (enforced by
    # tests/substrate/test_gpu_arbitration.py), so a new GPU service cannot ship unarbitrated.
    # See ordo/gpu.py.
    gpu_arbitration: GpuArbitration | None = None
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
    # compose `network_mode` (e.g. "service:caddy" — share another service's network namespace).
    # Used by the tailnet-name sidecars, whose `tailscale serve` can only proxy to 127.0.0.1, so
    # they join Caddy's netns and hit its port listeners on loopback. Mutually exclusive with
    # `networks:` — the renderer omits the network attachment when this is set.
    network_mode: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PluginService:
        name = str(d["name"])
        gpu_pin = str(d.get("gpu_pin", ""))
        return cls(
            name=name, image=str(d["image"]),
            gpu=bool(d.get("gpu", False)), gpu_pin=gpu_pin,
            # A pinned service defaults its contention device to the pin, so a manifest never
            # has to state the same fact twice (and the two can't disagree).
            gpu_arbitration=GpuArbitration.from_dict(
                d.get("gpu_arbitration"), service=name,
                default_device="secondary" if gpu_pin == "secondary" else "primary"),
            env={str(k): str(v) for k, v in (d.get("env", {}) or {}).items()},
            command=[str(c) for c in (d.get("command", []) or [])],
            volumes=[str(v) for v in (d.get("volumes", []) or [])],
            healthcheck=dict(d.get("healthcheck", {}) or {}),
            depends_on=[str(x) for x in (d.get("depends_on", []) or [])],
            wants_secrets=bool(d.get("wants_secrets", False)),
            ports=[str(p) for p in (d.get("ports", []) or [])],
            shm_size=str(d.get("shm_size", "")),
            network_mode=str(d.get("network_mode", "")),
        )


_MCP_ALLOWED_KEYS = frozenset({
    "server_id", "image", "url", "transport", "port", "path", "network", "command", "env", "volumes",
    "depends_on", "timeout", "auth", "allowed_tools", "tools", "healthcheck",
})


@dataclasses.dataclass(frozen=True)
class McpSpec:
    """The validated `mcp:` block of a kind=mcp plugin: ONE streamable-HTTP MCP server, either a
    compose service built from `image` (needs `port` + `healthcheck`, joins the internal MCP network)
    or a hosted `url` (no container). LiteLLM registers it by URL. stdio-only upstreams are bridged
    INSIDE their image (see services/codebase-memory/Dockerfile), never spawned by the gateway."""
    server_id: str
    image: str = ""
    url: str = ""
    transport: str = "http"
    port: int = 0
    path: str = "/mcp"
    network: str = "internal"        # internal: <project>-mcp-net only | stack: + <project>-net
    command: tuple[str, ...] = ()
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    volumes: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    timeout: int = 60                # LiteLLM per-server tool timeout (seconds)
    auth_type: str = ""              # "" | bearer_token | api_key (LiteLLM auth_type presented upstream)
    auth_secret: str = ""            # env var NAME (secrets.env) whose value LiteLLM presents
    allowed_tools: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()      # informational + parity-test expectation
    healthcheck: dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any], plugin_id: str) -> McpSpec:
        prefix = f"mcp plugin '{plugin_id}'"
        unknown = sorted(set(d) - _MCP_ALLOWED_KEYS)
        if unknown:
            raise ValueError(f"{prefix}: unknown mcp keys {unknown} (longLived/disableNetwork/PLACEHOLDER_* "
                             "are gone: declare transport/port/network/healthcheck)")
        transport = str(d.get("transport", "") or "")
        if transport != "http":
            raise ValueError(f"{prefix}: `transport: http` is required (got {transport!r}); stdio servers "
                             "are bridged inside their image with mcp-proxy")
        image = str(d.get("image", "") or "")
        url = str(d.get("url", "") or "")
        if bool(image) == bool(url):
            raise ValueError(f"{prefix}: declare exactly one of `image` (compose service) or `url` (hosted)")
        port = int(d.get("port", 0) or 0)
        healthcheck = dict(d.get("healthcheck", {}) or {})
        if image and port <= 0:
            raise ValueError(f"{prefix}: `port` (the container port serving the MCP path) is required")
        if image and not healthcheck:
            raise ValueError(f"{prefix}: a compose `healthcheck:` is required for an image-backed server")
        network = str(d.get("network", "internal") or "internal")
        if network not in ("internal", "stack"):
            raise ValueError(f"{prefix}: `network` must be internal or stack (got {network!r})")
        auth = dict(d.get("auth", {}) or {})
        auth_type = str(auth.get("type", "") or "")
        auth_secret = str(auth.get("secret", "") or "")
        if auth_type not in ("", "bearer_token", "api_key"):
            raise ValueError(f"{prefix}: auth.type must be bearer_token or api_key (got {auth_type!r})")
        if auth_type and not auth_secret:
            raise ValueError(f"{prefix}: auth.secret (the secrets.env var NAME) is required with auth.type")
        timeout = int(d.get("timeout", 60) or 60)
        if timeout <= 0:
            raise ValueError(f"{prefix}: timeout must be a positive number of seconds")
        return cls(
            server_id=str(d.get("server_id") or plugin_id),
            image=image, url=url, transport=transport, port=port,
            path=str(d.get("path", "/mcp") or "/mcp"), network=network,
            command=tuple(str(c) for c in (d.get("command", []) or [])),
            env={str(k): str(v) for k, v in (d.get("env", {}) or {}).items()},
            volumes=tuple(str(v) for v in (d.get("volumes", []) or [])),
            depends_on=tuple(str(x) for x in (d.get("depends_on", []) or [])),
            timeout=timeout, auth_type=auth_type, auth_secret=auth_secret,
            allowed_tools=tuple(str(t) for t in (d.get("allowed_tools", []) or [])),
            tools=tuple(str(t) for t in (d.get("tools", []) or [])),
            healthcheck=healthcheck,
        )

    @property
    def hosted(self) -> bool:
        return bool(self.url)

    @property
    def service_name(self) -> str:
        """Compose service name (`mcp-<server_id>`); empty for a hosted server."""
        return "" if self.hosted else f"mcp-{self.server_id}"

    def internal_url(self) -> str:
        """The URL LiteLLM dials: the hosted url, or the compose service on the internal network."""
        return self.url if self.hosted else f"http://{self.service_name}:{self.port}{self.path}"


@dataclasses.dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    description: str
    nvidia: bool
    vram_gb: float
    ram_gb: float
    depends_on: tuple[str, ...]
    provides: tuple[str, ...]   # documentation-only: resolve() matches literal plugin ids;
                                # no capability-based resolution consumes this (audit P2-37)
    compose_profile: str
    env: dict[str, str]
    kind: str = "service"          # "service" (compose service) | "mcp" (agent tool server)
    mcp: McpSpec | None = None     # the validated MCP server declaration for kind=mcp
    services: tuple[PluginService, ...] = ()  # compose services this plugin contributes (kind=service)
    # secret env KEYS this plugin's services need at runtime (names only, values operator-managed).
    # render emits these into secrets.env.example; the rendered compose reads them via a second
    # env_file `secrets.env` — derived (.env) config and operator secrets stay in separate files.
    secrets: tuple[str, ...] = ()
    # Build-context identity (METADATA for preflight/tests; NEVER rendered into compose). Absent ->
    # the plugin's own `services/<id>/` + `Dockerfile`. Declared only when the context isn't the
    # plugin's own dir. See ordo.buildspec.
    build: BuildSpec = dataclasses.field(default_factory=BuildSpec)
    # Optional per-consumer LiteLLM virtual key: {models: [group,...], mcp_servers: all|[server_id,...]}.
    # render derives the env var LITELLM_KEY_<ID>, adds it to required secrets, and emits the grant
    # into out/model-gateway/keys.json for bootstrap_keys.py. Empty -> this plugin gets no key.
    litellm_key: dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Plugin:
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
            mcp=(McpSpec.from_dict(dict(d.get("mcp", {}) or {}), plugin_id=str(d["id"]))
                 if str(d.get("kind", "service")) == "mcp" else None),
            services=tuple(PluginService.from_dict(s) for s in (d.get("services", []) or [])),
            secrets=tuple(str(s) for s in (d.get("secrets", []) or [])),
            build=BuildSpec.from_dict(d.get("build")),
            litellm_key=dict(d.get("litellm_key", {}) or {}),
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
    def load(cls, plugins_dir: str | Path) -> PluginRegistry:
        # Co-located manifests: each plugin declares itself in `services/<id>/plugin.yaml`.
        # sorted() over the glob keys the registry by path (== by folder id) so output order
        # is stable and independent of filesystem enumeration order.
        base = Path(plugins_dir)
        plugins = [
            Plugin.from_dict(yaml.safe_load(manifest.read_text(encoding="utf-8")) or {})
            for manifest in sorted(base.glob("*/plugin.yaml"))
        ]
        return cls(plugins)

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
