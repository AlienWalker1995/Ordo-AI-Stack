"""The render engine: (declarative source + hardware + catalog) -> RenderedConfig.

This is the drift cure. The ONE context-size value is computed once and flows to every
consumer (.env, Hermes, model-gateway) identically — they cannot disagree, because they're
all derived from the same source. Re-rendering overwrites any hand-edit to a derived output;
only `overrides:` in the source survives.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import yaml

from . import compose
from .agents import AgentRegistry
from .catalog import Catalog, DEFAULT_VRAM_RESERVE_GB, Model
from .config import Source
from .dashboards import DashboardRegistry
from .hardware import HardwareProfile, detect
from .plugins import PluginRegistry

DEFAULT_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"
DEFAULT_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
DEFAULT_DASHBOARDS_DIR = Path(__file__).resolve().parent.parent / "dashboards"

# Secret env KEYS the CORE services need at runtime (values operator-managed in secrets.env, never
# rendered). model-gateway/mcp-gateway/ops-controller/dashboard/agent read these; plugins add more
# via their manifest `secrets:` list. Mirrors the V1 SOPS-decrypted runtime/.env surface.
CORE_SECRET_KEYS: tuple[str, ...] = (
    "LITELLM_MASTER_KEY",         # model-gateway master key (LiteLLM)
    "OPS_CONTROLLER_TOKEN",       # bearer between agent/dashboard/mcp ↔ ops-controller
    "DASHBOARD_AUTH_TOKEN",       # dashboard API bearer
    "THROUGHPUT_RECORD_TOKEN",    # model-gateway → dashboard throughput samples
    "HF_TOKEN",                   # Hugging Face (gated model pulls)
    "GITHUB_PERSONAL_ACCESS_TOKEN",  # mcp-gateway GitHub MCP + ComfyUI-Manager
)

# Deep-merge an override dict onto a derived dict (overrides win, survive regeneration).
def _apply_overrides(derived: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    out = dict(derived)
    for k, v in (overrides or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _apply_overrides(out[k], v)
        else:
            out[k] = v
    return out


def _max_ctx_for_vram(model: Model, hw: HardwareProfile, reserve_gb: float) -> int:
    """Largest context that fits after weights + reserve, capped at the model's trained ctx.

    Encodes the KV-math lesson: KV grows ~linearly with ctx, so on a smaller card we cut
    ctx rather than spill. Falls back to ctx_default when we can't estimate (CPU / no kv rate).
    """
    if not hw.has_gpu or not model.kv_kb_per_token:
        return model.ctx_default
    free_after_weights_gb = hw.primary_vram_gb - model.vram_gb - reserve_gb
    if free_after_weights_gb <= 0:
        return min(model.ctx_default, 8192)
    max_tokens = int((free_after_weights_gb * 1024 * 1024) / model.kv_kb_per_token)
    # round down to a tidy multiple of 8K
    max_tokens = (max_tokens // 8192) * 8192
    return max(8192, min(model.ctx_default, max_tokens))


@dataclasses.dataclass
class RenderedConfig:
    hardware: HardwareProfile
    model: Model
    ctx_size: int
    tier: str
    warnings: list[str]
    env: dict[str, str]
    hermes: dict[str, Any]
    model_gateway: dict[str, Any]
    # Selected control-plane UI wiring (data-driven, like `hermes` for the agent). Carries the
    # dashboard service's image/env/depends/healthcheck + an OPTIONAL backend service (ops-api).
    dashboard: dict[str, Any]
    plugins_enabled: list[str]
    compose_profiles: list[str] = dataclasses.field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # (plugin, service) pairs for every enabled kind=service plugin — compose builds from these
    plugin_services: list[Any] = dataclasses.field(default_factory=list)
    # secret env KEYS the enabled services need (core + plugins). Values are NEVER rendered — they
    # live in an operator-managed secrets.env; write() emits secrets.env.example (keys only).
    required_secrets: list[str] = dataclasses.field(default_factory=list)

    def resident_vram_gb(self) -> float:
        """The GPU footprint the resident LLM actually holds while cached: weights + KV at the
        rendered context. This is the value the scheduler registers llama.cpp with as an evictable
        idle resident — computed from the SAME render the stack runs, so it can't drift from what
        `.env` tells llama.cpp to load. Weights-only (the catalog `vram_gb`) understates residency
        and would make the scheduler think a media job fits beside the LLM when it doesn't; adding
        the KV cache (ctx * kv_kb_per_token) gives the true footprint that must be freed for a lease.
        """
        weights = float(self.model.vram_gb)
        kv_kb = self.model.kv_kb_per_token or 0.0
        kv_gb = (self.ctx_size * kv_kb) / (1024.0 * 1024.0)  # ctx tokens * KB/token -> GB
        return round(weights + kv_gb, 2)

    def manifest(self) -> dict[str, Any]:
        return {
            "hardware": self.hardware.summary(),
            "tier": self.tier,
            "model": {"id": self.model.id, "file": self.model.file, "vram_gb": self.model.vram_gb,
                      "resident_vram_gb": self.resident_vram_gb()},
            "ctx_size": self.ctx_size,
            "plugins_enabled": self.plugins_enabled,
            "compose_profiles": self.compose_profiles,
            "mcp_servers": [s["id"] for s in self.mcp_servers],
            "warnings": self.warnings,
            "derived": {
                "env.LLAMACPP_CTX_SIZE": self.env["LLAMACPP_CTX_SIZE"],
                "hermes.context_length": self.hermes["context_length"],
                "model_gateway.ctx": self.model_gateway["ctx"],
            },
        }

    def compose_dict(self, project: str = "ordo") -> dict[str, Any]:
        """The isolated, runnable compose for the v2 stack — built from the resolved plugin
        services (data-driven), with the primary- AND secondary-GPU uuids resolved for the pins."""
        pri = self.hardware.primary_gpu
        sec = self.hardware.secondary_gpu
        return compose.render_compose(
            has_gpu=self.hardware.has_gpu, compose_profiles=self.compose_profiles,
            agent=self.hermes.get("agent", "hermes"), project=project,
            agent_image=self.hermes.get("agent_image") or None,
            agent_command=self.hermes.get("agent_command") or None,
            agent_user=self.hermes.get("agent_user") or None,
            agent_volumes=self.hermes.get("agent_volumes") or None,
            agent_environment=self.hermes.get("agent_environment") or None,
            agent_secret_files=self.hermes.get("agent_secret_files") or None,
            agent_depends_on=self.hermes.get("agent_depends_on") or None,
            agent_healthcheck=self.hermes.get("agent_healthcheck") or None,
            dashboard=self.dashboard,
            llamacpp_image=self.env.get("LLAMACPP_IMAGE") or None,
            plugin_services=self.plugin_services,
            primary_gpu_uuid=(pri.uuid if pri else None),
            secondary_gpu_uuid=(sec.uuid if sec else None))

    def write(self, out_dir: str | Path) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        # .env (derived — regenerated every time; hand-edits here do not survive)
        env_lines = ["# GENERATED by ordo render — do not hand-edit; change ordo.yaml instead"]
        env_lines += [f"{k}={v}" for k, v in sorted(self.env.items())]
        (out / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        (out / "hermes.context.json").write_text(json.dumps(self.hermes, indent=2), encoding="utf-8")
        (out / "manifest.json").write_text(json.dumps(self.manifest(), indent=2), encoding="utf-8")
        # secrets.env.example — the KEYS the enabled stack needs, values EMPTY. The operator copies
        # this to secrets.env and fills real values (SOPS-decrypted / hand-set). Derived config
        # (.env) and secrets stay in separate files; secrets.env is NOT rendered/overwritten.
        sec_lines = [
            "# GENERATED by ordo render — secret KEYS the enabled stack needs (values EMPTY).",
            "# Copy to `secrets.env` and fill real values (never commit secrets.env). The rendered",
            "# compose reads secrets.env as a second env_file for services that need secrets.",
        ]
        sec_lines += [f"{k}=" for k in self.required_secrets]
        (out / "secrets.env.example").write_text("\n".join(sec_lines) + "\n", encoding="utf-8")
        # the mcp-gateway registry, regenerated from kind=mcp plugins (no hand-edit, no drift). Kept
        # as a human-readable summary; the LOAD-BEARING config the gateway wrapper actually reads is
        # the wrapper-native pair below (servers.txt + registry-custom.yaml) under out/mcp/.
        (out / "mcp-registry.yaml").write_text(
            yaml.safe_dump({"servers": self.mcp_servers}, sort_keys=False), encoding="utf-8")
        # mcp/ — the config dir mounted into mcp-gateway at /mcp-config. The wrapper
        # (gateway-wrapper.sh) reads servers.txt (the enabled MCP ids) and merges registry-custom.yaml
        # as an --additional-catalog. Emitted in EXACTLY the schema V1's wrapper consumes, so the same
        # wrapper works unmodified — the rendered artifact matches its reader (no drift).
        mcp_dir = out / "mcp"
        mcp_dir.mkdir(parents=True, exist_ok=True)
        (mcp_dir / "servers.txt").write_text(
            ",".join(s["id"] for s in self.mcp_servers) + "\n", encoding="utf-8")
        (mcp_dir / "registry-custom.yaml").write_text(
            _render_registry_custom(self.mcp_servers), encoding="utf-8")
        # an isolated, runnable compose for the v2 stack (own project/network, no port clashes)
        (out / "docker-compose.yml").write_text(
            yaml.safe_dump(self.compose_dict(), sort_keys=False),
            encoding="utf-8")


def _resolve_hardware(source: Source) -> HardwareProfile:
    if source.hardware == "auto" or source.hardware is None:
        return detect()
    return HardwareProfile.from_spec(source.hardware)


def render(source: Source, catalog: Catalog,
           plugins: PluginRegistry | None = None,
           reserve_gb: float = DEFAULT_VRAM_RESERVE_GB,
           agents: AgentRegistry | None = None,
           dashboards: DashboardRegistry | None = None) -> RenderedConfig:
    hw = _resolve_hardware(source)
    model, warnings = catalog.resolve(hw, source.model, source.tier, reserve_gb)
    if plugins is None:
        plugins = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    if agents is None:
        agents = AgentRegistry.load(DEFAULT_AGENTS_DIR)
    if dashboards is None:
        dashboards = DashboardRegistry.load(DEFAULT_DASHBOARDS_DIR)

    ctx = _max_ctx_for_vram(model, hw, reserve_gb)

    # --- one source value → every consumer (this is the whole point) ---
    derived: dict[str, Any] = {
        "llamacpp": {
            "ctx_size": ctx,
            "model": model.file,
            "gpu_layers": -1 if hw.has_gpu else 0,
            "kv_cache_type": "q8_0",
            "parallel": 1,
            "flash_attn": "auto",
            "rope_scaling": "none",
            "rope_scale": 1,
            "yarn_orig_ctx": 0,
            "n_predict": 65536,
            "reasoning_budget": 32768,
            "enable_kv_quant": 1,
            "mmproj": model.mmproj or "",
            "extra_args": model.extra_args,
            "image": model.backend_image or "",
        },
    }
    # `overrides:` survive regeneration; everything else is recomputed each render.
    derived = _apply_overrides(derived, source.overrides)
    lc = derived["llamacpp"]
    ctx = int(lc["ctx_size"])  # re-read in case an override pinned it

    env = {
        "LLAMACPP_MODEL": str(lc["model"]),
        "LLAMACPP_CTX_SIZE": str(ctx),
        "LLAMACPP_GPU_LAYERS": str(lc["gpu_layers"]),
        "LLAMACPP_PARALLEL": str(lc["parallel"]),
        "LLAMACPP_FLASH_ATTN": str(lc["flash_attn"]),
        "LLAMACPP_ROPE_SCALING": str(lc["rope_scaling"]),
        "LLAMACPP_ROPE_SCALE": str(lc["rope_scale"]),
        "LLAMACPP_YARN_ORIG_CTX": str(lc["yarn_orig_ctx"]),
        "LLAMACPP_N_PREDICT": str(lc["n_predict"]),
        "LLAMACPP_REASONING_BUDGET": str(lc["reasoning_budget"]),
        "LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION": str(lc["enable_kv_quant"]),
        "LLAMACPP_KV_CACHE_TYPE_K": str(lc["kv_cache_type"]),
        "LLAMACPP_KV_CACHE_TYPE_V": str(lc["kv_cache_type"]),
        "LLAMACPP_MMPROJ": str(lc["mmproj"]),
        "LLAMACPP_EXTRA_ARGS": str(lc["extra_args"]),
    }
    # Only surface a backend-image override when the model declares one; the default image
    # lives in compose.render_compose, so an empty var here would just be noise/drift.
    if lc["image"]:
        env["LLAMACPP_IMAGE"] = str(lc["image"])
    # Resolve the chosen agent from the registry (Hermes is the default). Unknown id -> a warning
    # + the naming convention, so a typo surfaces at render/preflight not at compose-up.
    agent, agent_notes = agents.resolve(source.agent)
    warnings = warnings + agent_notes
    agent_image = agent.image_for("ordo") if agent else ""
    agent_command = list(agent.command) if agent else []
    hermes = {
        "context_length": ctx, "agent": source.agent, "agent_image": agent_image,
        "agent_command": agent_command,
        # Full runtime wiring for the agent service (data-driven parity with the V1 container).
        # Empty/absent for an agent that declares none — compose omits each accordingly.
        "agent_user": (agent.user if agent else ""),
        "agent_volumes": (list(agent.volumes) if agent else []),
        "agent_environment": (dict(agent.environment) if agent else {}),
        "agent_secret_files": ([dict(s) for s in agent.secret_files] if agent else []),
        "agent_depends_on": (dict(agent.depends_on) if agent else {}),
        "agent_healthcheck": (dict(agent.healthcheck) if agent else {}),
    }
    model_gateway = {"ctx": ctx, "model_id": "local-chat"}

    # Resolve the chosen control-plane UI from the registry (v2-native is the default). Unknown id ->
    # a warning + fall back to the default, so a typo surfaces at render/preflight. The selected
    # dashboard flows its image/env/depends/healthcheck (+ an optional backend service) into compose.
    dash, dash_notes = dashboards.resolve(source.dashboard)
    warnings = warnings + dash_notes
    dashboard: dict[str, Any] = {"id": source.dashboard}
    if dash:
        dashboard = {
            "id": dash.id,
            "image": dash.image_for("ordo"),
            "environment": dict(dash.environment),
            "volumes": list(dash.volumes),
            "depends_on": dict(dash.depends_on),
            "healthcheck": dict(dash.healthcheck),
            "wants_secrets": dash.wants_secrets,
            "gpu_capabilities": list(dash.gpu_capabilities),
            "backend": None,
        }
        if dash.backend and dash.backend.name:
            b = dash.backend
            dashboard["backend"] = {
                "name": b.name,
                "image": b.image_for("ordo"),
                "environment": dict(b.environment),
                "volumes": list(b.volumes),
                "depends_on": dict(b.depends_on),
                "healthcheck": dict(b.healthcheck),
                "group_add_root": b.group_add_root,
                "wants_secrets": b.wants_secrets,
                "gpu_capabilities": list(b.gpu_capabilities),
            }

    # Registry-driven plugin resolution: enable what's requested AND fits AND has its deps.
    enabled, notes = plugins.resolve(source.plugins, hw)
    warnings = warnings + notes
    services = [p for p in enabled if p.kind == "service"]
    mcps = [p for p in enabled if p.kind == "mcp"]
    for p in services:
        env.update(p.env)  # plugin-level env fragment goes to the rendered .env
    compose_profiles = sorted({p.compose_profile for p in services if p.compose_profile})
    # flatten to (plugin, service) pairs — compose builds each declared service from data
    plugin_services = [(p, ps) for p in services for ps in p.services]
    mcp_servers, mcp_notes = _render_mcp(mcps)

    # Host/site config (DATA_PATH/BASE_PATH/CODE_ROOT, edge hostnames, COMFYUI_IMAGE, …) flows
    # verbatim into .env so plugin `${VAR}` refs resolve deterministically. Derived keys WIN over
    # site (a site DATA_PATH can't shadow a computed LLAMACPP_CTX_SIZE) — protects the drift gate.
    for k, v in (source.site or {}).items():
        env.setdefault(str(k), str(v))

    # Secret KEYS the enabled stack needs: the always-present core set + each enabled plugin's
    # declared `secrets:`. Deduped, core-first order preserved. Values never rendered.
    required_secrets = list(CORE_SECRET_KEYS)
    for p in enabled:
        for key in p.secrets:
            if key not in required_secrets:
                required_secrets.append(key)

    return RenderedConfig(
        hardware=hw, model=model, ctx_size=ctx, tier=(model.tier),
        warnings=warnings + mcp_notes, env=env, hermes=hermes, model_gateway=model_gateway,
        dashboard=dashboard,
        plugins_enabled=[p.id for p in services], compose_profiles=compose_profiles,
        mcp_servers=mcp_servers, plugin_services=plugin_services,
        required_secrets=required_secrets,
    )


def _is_project_image(image: str, project: str = "ordo") -> bool:
    """A locally-BUILT project MCP image (e.g. ordo/qdrant-rag-mcp:latest). It has no public
    registry to digest-pin against — it's pinned by its build context (like llamacpp-patched), so
    it's reproducible without an @sha256. Preflight surfaces it as 'build first', not a leak risk."""
    return image.startswith(f"{project}/")


def _render_registry_custom(mcp_servers: list[dict[str, Any]]) -> str:
    """Emit the gateway wrapper's `--additional-catalog` fragment (registry-custom.yaml) from the
    rendered kind=mcp servers. Schema mirrors V1's data/mcp/registry-custom.yaml EXACTLY: a top-level
    `registry:` map keyed by server id, each with type/title/description/image + env as a list of
    {name, value}. The wrapper substitutes any PLACEHOLDER_* tokens at startup; we emit concrete
    values from the manifest so there are none to substitute (secrets stay in secrets.env env-vars).

    File-based MCP servers (e.g. a markdown-vault reader/writer) additionally need a host bind for
    their data dir, must stay warm across calls, and — being pure-fs — can run offline. The upstream
    docker/mcp-gateway catalog schema (verified in the docker-mcp binary: yaml keys `volumes`,
    `command`, `longLived`, `disableNetwork`) carries these, so we PASS THEM THROUGH when a manifest
    declares them. `volumes` are host mount specs applied to the SPAWNED sibling container: the source
    is a HOST path (the gateway spawns via the host docker.sock, so a gateway-container path would be
    wrong), typically a `PLACEHOLDER_*` token the wrapper substitutes from the gateway's env. A host
    bind without a `:ro` suffix is READ-WRITE — that is how a vault-writing MCP gets write access
    (named-volume-only would hide the vault from the host + native Obsidian browsing the same dir)."""
    registry: dict[str, Any] = {}
    for s in mcp_servers:
        entry: dict[str, Any] = {
            "type": "server",
            "title": s.get("name", s["id"]),
            "description": s.get("name", s["id"]),
            "image": s.get("image", ""),
            "env": [{"name": k, "value": v} for k, v in (s.get("env", {}) or {}).items()],
        }
        # Optional catalog fields — only emitted when the manifest declares them, so existing MCP
        # plugins (image+env only) render byte-identically. Order after env for stable diffs.
        if s.get("volumes"):
            entry["volumes"] = list(s["volumes"])
        if s.get("command"):
            entry["command"] = list(s["command"])
        if s.get("longLived"):
            entry["longLived"] = True
        if s.get("disableNetwork"):
            entry["disableNetwork"] = True
        registry[s["id"]] = entry
    header = (
        "# GENERATED by ordo render — the mcp-gateway --additional-catalog fragment.\n"
        "# Rebuilt from the enabled kind=mcp plugins; do not hand-edit (change plugins/*/plugin.yaml).\n"
    )
    return header + yaml.safe_dump({"registry": registry}, sort_keys=False)


def _render_mcp(mcps: list, project: str = "ordo") -> tuple[list[dict[str, Any]], list[str]]:
    """Build the mcp-gateway registry from kind=mcp plugins. Public images MUST be digest-pinned
    (no Docker online-catalog roulette — the leak/drift source V1 suffered). Locally-built
    project images (ordo/*) are exempt: they're pinned by build context, not registry digest."""
    servers: list[dict[str, Any]] = []
    notes: list[str] = []
    seen_ids: dict[str, str] = {}
    for p in mcps:
        image = str(p.mcp.get("image", ""))
        digest = image.split("@sha256:")[-1] if "@sha256:" in image else ""
        if _is_project_image(image, project):
            pass  # locally built — pinned by its build context, not a registry digest
        elif not digest:
            notes.append(f"mcp '{p.id}': image is not digest-pinned — refuse in production")
        elif len(set(digest)) <= 1:  # placeholder like 000.../111...
            notes.append(f"mcp '{p.id}': image digest is a placeholder — set the real sha256")
        # The gateway registry key (servers.txt id + tool-namespace prefix Hermes sees, e.g.
        # `comfyui__system_stats`) defaults to the plugin id. A plugin may set mcp.server_id to
        # DECOUPLE that key from its plugin id — needed when a kind=service plugin already owns the
        # bare name (the comfyui SERVICE plugin owns `comfyui`, so its MCP plugin is id `comfyui-mcp`
        # but keeps server_id `comfyui` to preserve V1's `comfyui__*` tool namespace).
        server_id = str(p.mcp.get("server_id") or p.id)
        if server_id in seen_ids:
            notes.append(f"mcp '{p.id}': server_id '{server_id}' collides with plugin '{seen_ids[server_id]}'")
        seen_ids[server_id] = p.id
        servers.append({
            "id": server_id, "name": p.name, "image": image,
            "env": dict(p.mcp.get("env", {}) or {}),
            "tools": list(p.mcp.get("tools", []) or []),
            # Optional gateway-catalog passthrough (file-based MCP servers): a host bind for the data
            # dir (RW when no `:ro`), an explicit container command, keep-warm, and offline lockdown.
            # Absent on the existing image+env MCP plugins, so their rendered entry is unchanged.
            "volumes": list(p.mcp.get("volumes", []) or []),
            "command": list(p.mcp.get("command", []) or []),
            "longLived": bool(p.mcp.get("longLived", False)),
            "disableNetwork": bool(p.mcp.get("disableNetwork", False)),
        })
    return servers, notes
