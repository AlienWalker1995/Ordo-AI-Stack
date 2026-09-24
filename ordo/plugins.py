"""Plugin registry — plugins declare their hardware needs + config fragment (data, not code).

The renderer reads manifests and composes enabled plugins into the rendered config. A plugin
is enabled only if it's requested (auto/explicit), its hardware requirements are met, the site
keys it requires are set, AND its declared dependencies are also enabled. Media plugins
self-declare NVIDIA-only.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
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
    # Start-order peers. A LIST is plain ordering (compose's short form). A MAPPING
    # {peer: condition} renders the long form, so a service can wait for a peer to be READY
    # (`service_healthy`) rather than merely created - the shape agent.yaml already uses, and what
    # an application service needs when its datastores must finish migrating/starting first.
    depends_on: list[str] | dict[str, str] = dataclasses.field(default_factory=list)
    # The secret NAMES this service reads. Each renders as `KEY: ${KEY}` in its environment, and
    # compose interpolates the value from `--env-file secrets.env`, so a service holds only the
    # secrets it needs. Secret VALUES never live in the rendered config, only the reference.
    secrets: tuple[str, ...] = ()
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
    # compose `entrypoint` - REPLACES the image's baked ENTRYPOINT (`command` only replaces CMD).
    # Exec form (a list) so there is no shell word-splitting ambiguity; a shell one-liner is
    # written explicitly as ["sh", "-c", "…"]. Needed by images that must run a setup step before
    # their server (langfuse-minio pre-creates its bucket). Empty -> the image's own entrypoint.
    entrypoint: list[str] = dataclasses.field(default_factory=list)
    # compose `security_opt` (e.g. ["no-new-privileges:true"]). The MCP services get this from the
    # renderer; a plugin service declares it here so an ordinary service can opt into the same
    # hardening without a per-plugin if-block in compose.py.
    security_opt: list[str] = dataclasses.field(default_factory=list)
    # compose `ulimits` - passed through verbatim (e.g. {"nofile": {"soft": 262144, "hard": 262144}}).
    # ClickHouse needs a raised file-descriptor ceiling; docker's default (1024) makes it log
    # "Too many open files" under load.
    ulimits: dict[str, Any] = dataclasses.field(default_factory=dict)
    # CPU / memory CEILINGS -> compose `deploy.resources.limits` (e.g. {"cpus": "2", "memory": "4g"}).
    # Same shape the renderer already gives every MCP service. Merged with (never replacing) a GPU
    # `reservations` block, so a limited GPU service keeps its device reservation.
    resources: dict[str, str] = dataclasses.field(default_factory=dict)
    # compose `restart` policy. Empty -> the renderer's default `unless-stopped` (a long-lived
    # service). A one-shot init step declares `on-failure`, so a transient error is retried but a
    # clean exit stays exited instead of being restarted forever (model-gateway-keys' policy).
    restart: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PluginService:
        name = str(d["name"])
        where = f"service {name!r}"
        if "wants_secrets" in d:
            raise ValueError(
                f"{where}: `wants_secrets` was replaced by `secrets: [NAMES]`. A service now lists the "
                "secret names it reads, instead of receiving the whole secrets.env")
        gpu_pin = str(d.get("gpu_pin", ""))
        raw_depends = d.get("depends_on", []) or []
        depends_on: list[str] | dict[str, str] = (
            {str(k): str(v) for k, v in raw_depends.items()} if isinstance(raw_depends, dict)
            else [str(x) for x in raw_depends]
        )
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
            depends_on=depends_on,
            secrets=tuple(str(k) for k in (d.get("secrets", []) or [])),
            ports=[str(p) for p in (d.get("ports", []) or [])],
            shm_size=str(d.get("shm_size", "")),
            network_mode=str(d.get("network_mode", "")),
            entrypoint=[str(e) for e in (d.get("entrypoint", []) or [])],
            security_opt=[str(o) for o in (d.get("security_opt", []) or [])],
            ulimits=dict(d.get("ulimits", {}) or {}),
            resources={str(k): str(v) for k, v in (d.get("resources", {}) or {}).items()},
            restart=_restart_policy(name, d.get("restart", "")),
        )


# The compose restart policies a plugin service may declare. `always` is left out on purpose: it
# restarts a one-shot that exited cleanly, which is exactly the loop `on-failure` exists to avoid.
_RESTART_POLICIES = frozenset({"no", "on-failure", "unless-stopped"})


def _restart_policy(service: str, raw: Any) -> str:
    """Validate a manifest `restart:` value ("" keeps the renderer default)."""
    # YAML reads a bare `no` as False; accept it as the policy it was meant to be.
    value = "no" if raw is False else str(raw or "")
    if value and value not in _RESTART_POLICIES:
        raise ValueError(f"service '{service}': restart must be one of {sorted(_RESTART_POLICIES)} (got {value!r})")
    return value


_MCP_ALLOWED_KEYS = frozenset({
    "server_id", "image", "url", "transport", "port", "path", "network", "command", "env", "volumes",
    "depends_on", "timeout", "auth", "allowed_tools", "tools", "healthcheck",
})


@dataclasses.dataclass(frozen=True)
class McpSpec:
    """The validated `mcp:` block of a kind=mcp plugin: ONE streamable-HTTP MCP server, either a
    compose service built from `image` (needs `port`, joins the internal MCP network; `healthcheck`
    is an OPTIONAL override of the renderer's default HTTP probe)
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
        # No `healthcheck:` required: the renderer emits compose.default_mcp_healthcheck(port, path)
        # for an image-backed server. A manifest healthcheck stays available as an OVERRIDE, for an
        # image that cannot run the default probe (searxng-mcp ships node, not python3).
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
    def litellm_name(self) -> str:
        """The name LiteLLM knows this server by: `server_id` with hyphens turned into underscores.

        LiteLLM rejects any MCP server name containing MCP_TOOL_PREFIX_SEPARATOR (default `-`,
        validate_mcp_server_name), because it namespaces tools to clients as `<litellm_name>-<tool>`.
        The hyphenated `server_id` stays the stack-side identity (compose service `mcp-<server_id>`,
        compose labels, the dashboard's plugin map); this derived name is LiteLLM's only."""
        return self.server_id.replace("-", "_")

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
    # Does `plugins: auto` enable this one? `default: false` makes the plugin OPT-IN: it runs only
    # when the source lists it by id. The hardware gate (`requires:`) is the wrong instrument for a
    # plugin whose real cost is a standing multi-container footprint rather than a GPU or RAM floor
    # - langfuse runs six containers on any hardware, so "the box can run it" must not mean "every
    # box should". Defaults to True, so every existing manifest keeps its current behaviour.
    default: bool = True
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
    # `requires.site`: the operator `site:` keys this plugin cannot run without (its compose fails
    # loud, `${KEY:?}`, on an empty value). `plugins: auto` skips the plugin while any is missing;
    # an explicit `plugins:` list that names it is a render error.
    site_keys: tuple[str, ...] = ()

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
            default=bool(d.get("default", True)),
            kind=str(d.get("kind", "service")),
            mcp=(McpSpec.from_dict(dict(d.get("mcp", {}) or {}), plugin_id=str(d["id"]))
                 if str(d.get("kind", "service")) == "mcp" else None),
            services=tuple(PluginService.from_dict(s) for s in (d.get("services", []) or [])),
            secrets=tuple(str(s) for s in (d.get("secrets", []) or [])),
            build=BuildSpec.from_dict(d.get("build")),
            litellm_key=dict(d.get("litellm_key", {}) or {}),
            site_keys=tuple(str(k) for k in (req.get("site", []) or [])),
        )

    @property
    def needs_secondary_gpu(self) -> bool:
        """True if ANY of this plugin's services must be pinned to a non-primary GPU
        (its image has no kernels for the primary card, so primary-fallback would ship a crash)."""
        return any(s.gpu_pin == "secondary" for s in self.services)

    def fits(self, hw: HardwareProfile) -> bool:
        # `nvidia: true` means an NVIDIA compute card, not just any GPU: these images are CUDA
        # builds and compose can only reserve NVIDIA devices.
        if self.nvidia and not hw.primary_is_nvidia:
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

    def missing_site_keys(self, site: Mapping[str, Any]) -> list[str]:
        """The required site keys that are absent or blank in `site`, in manifest order."""
        return [key for key in self.site_keys if not str(site.get(key, "") or "").strip()]


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
        self, requested: Any, hw: HardwareProfile, site: Mapping[str, Any] | None = None,
    ) -> tuple[list[Plugin], list[str]]:
        """Return (enabled plugins, notes). 'auto' = everything the hardware can run, MINUS the
        opt-in plugins (`default: false`), which only an explicit `plugins:` list can enable.

        `site` is the source's `site:` block. When given, a plugin missing a required site key is
        skipped under 'auto' (with a note naming the keys), and an explicit list that names one
        raises ValueError. None leaves the site gate out, for callers asking only about hardware."""
        notes: list[str] = []
        if requested == "auto" or requested is None:
            wanted = {p.id for p in self.plugins if p.default}
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

        # site gate: a plugin whose compose refuses to run without a site key stays off until set
        if site is not None:
            missing_by_plugin = {pid: p.missing_site_keys(site) for pid, p in enabled.items()}
            missing_by_plugin = {pid: keys for pid, keys in missing_by_plugin.items() if keys}
            if missing_by_plugin and requested not in ("auto", None):
                problems = "; ".join(f"'{pid}' needs {', '.join(keys)}"
                                     for pid, keys in sorted(missing_by_plugin.items()))
                raise ValueError(f"required site key(s) missing: {problems}. Set them under `site:` "
                                 "in ordo.yaml, or remove the plugin from `plugins:`, then re-run "
                                 "`ordo render`")
            for pid, keys in sorted(missing_by_plugin.items()):
                notes.append(f"'{pid}' not enabled - required site key(s) {', '.join(keys)} not set; "
                             "set them under `site:` in ordo.yaml and re-run `ordo render`")
                del enabled[pid]

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
