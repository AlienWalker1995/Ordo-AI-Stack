"""The render engine: (declarative source + hardware + catalog) -> RenderedConfig.

This is the drift cure. The ONE context-size value is computed once and flows to every
consumer (.env, Hermes, model-gateway) identically — they cannot disagree, because they're
all derived from the same source. Re-rendering overwrites any hand-edit to a derived output;
only `overrides:` in the source survives.
"""
from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any

import yaml

from . import compose, gpu
from .agents import AgentRegistry
from .catalog import DEFAULT_VRAM_RESERVE_GB, Catalog, Model
from .config import Source
from .dashboards import DashboardRegistry
from .hardware import HardwareProfile, detect
from .plugins import PluginRegistry

# Render data now lives co-located under services/<id>/ (plugin.yaml / agent.yaml / dashboard.yaml
# / catalog.json); each registry globs its own manifest kind out of the shared services/ root.
DEFAULT_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "services"
DEFAULT_AGENTS_DIR = Path(__file__).resolve().parent.parent / "services"
DEFAULT_DASHBOARDS_DIR = Path(__file__).resolve().parent.parent / "services"

# Gate-enforced service -> the .env key its in-stack consumers already use for its base URL.
# When the service is gated, render points that key at the gate so mcp-gateway, comfyui-mcp, the
# dashboard and ops-api all submit through arbitration instead of around it. Guarded by
# tests/substrate/test_gpu_arbitration.py (every gated service must appear here, or its consumers
# would silently keep the direct route).
GATED_SERVICE_URL_ENV: dict[str, str] = {"comfyui": "COMFYUI_URL"}

# Secret env KEYS the CORE services need at runtime (values operator-managed in secrets.env, never
# rendered). model-gateway/mcp-gateway/ops-controller/dashboard/agent read these; plugins add more
# via their manifest `secrets:` list. Mirrors the V1 SOPS-decrypted runtime/.env surface.
CORE_SECRET_KEYS: tuple[str, ...] = (
    "LITELLM_MASTER_KEY",         # model-gateway master key (LiteLLM admin + UI login)
    "LITELLM_SALT_KEY",           # LiteLLM DB credential-encryption salt. NEVER rotate (stored creds unreadable)
    "LITELLM_DB_PASSWORD",        # litellm-db postgres password (compose-interpolated into DATABASE_URL)
    "OPS_CONTROLLER_TOKEN",       # bearer between agent/dashboard/mcp <-> ops-controller
    # NB: no DASHBOARD_AUTH_TOKEN — the dashboard has NO per-service auth. The Caddy edge
    # (oauth2-proxy + Google SSO) is the ONLY gate; internal callers reach it over ordo-net.
    # (operator mandate: auth is the edge's job, not baked into every service — 2026-07-15.)
    # NB: THROUGHPUT_RECORD_TOKEN is intentionally NOT required. There is no SOPS source that can
    # supply it, and the dashboard only enforces it "when set" (dashboard/app.py) — the /api/
    # throughput/record route is open when the var is empty. Demanding a key nothing can provide
    # would make secrets.env.example (and any preflight secrets-completeness check) list an
    # unfulfillable key. It stays an OPTIONAL var: set it to harden the internal route, or leave
    # it unset. (2026-07-24 hardening audit.)
    "HF_TOKEN",                   # Hugging Face (gated model pulls)
    "GITHUB_PERSONAL_ACCESS_TOKEN",  # ComfyUI-Manager (git-based node installs)
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


def key_env_name(consumer_id: str) -> str:
    """`open-webui` -> `LITELLM_KEY_OPEN_WEBUI` (the secrets.env var carrying that consumer's key)."""
    return "LITELLM_KEY_" + re.sub(r"[^A-Za-z0-9]", "_", consumer_id).upper()


def render_litellm_keys(consumers: list[tuple[str, dict[str, Any]]],
                        server_ids: list[str]) -> list[dict[str, Any]]:
    """Turn each consumer's `litellm_key:` declaration into a key grant for bootstrap_keys.py.
    `mcp_servers: all` expands to every ENABLED server id (sorted); a list may name only enabled
    ids, else the render fails (a typo must not silently grant nothing)."""
    keys: list[dict[str, Any]] = []
    known = sorted(server_ids)
    for consumer_id, spec in consumers:
        if not spec:
            continue
        models = [str(m) for m in (spec.get("models") or [])]
        raw = spec.get("mcp_servers", [])
        if raw == "all":
            granted = list(known)
        else:
            granted = sorted(str(s) for s in (raw or []))
            unknown = [s for s in granted if s not in known]
            if unknown:
                raise ValueError(f"litellm_key for '{consumer_id}' grants unknown/disabled MCP servers: {unknown}")
        keys.append({"env": key_env_name(consumer_id), "alias": consumer_id,
                     "models": models, "mcp_servers": granted})
    return keys


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
    # server_id → plugin_id for EVERY registered kind=mcp plugin (enabled AND available-but-disabled).
    # Emitted into out/mcp/servers.json (the dir the dashboard mounts). The dashboard reads it to
    # translate a UI MCP toggle back to the ordo.yaml plugin it must add/remove, so the toggle updates
    # the source of truth and PERSISTS across a re-render (which regenerates the enabled roster from
    # that same list). Covers disabled plugins too, so the UI can re-enable an available one.
    mcp_server_plugin_map: dict[str, str] = dataclasses.field(default_factory=dict)
    # (plugin, service) pairs for every enabled kind=service plugin — compose builds from these
    plugin_services: list[Any] = dataclasses.field(default_factory=list)
    # secret env KEYS the enabled services need (core + plugins). Values are NEVER rendered — they
    # live in an operator-managed secrets.env; write() emits secrets.env.example (keys only).
    required_secrets: list[str] = dataclasses.field(default_factory=list)
    # Per-consumer LiteLLM virtual-key grants declared by the agent/plugin manifests
    # (`litellm_key:`). Rendered to out/model-gateway/keys.json, which the model-gateway-keys
    # one-shot reads to provision the keys against the running LiteLLM proxy.
    litellm_keys: list[dict[str, Any]] = dataclasses.field(default_factory=list)

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

    def gpu_inventory(self) -> list[gpu.GpuClaim]:
        """Every DECLARED claim on a GPU: core services + the enabled plugins' services.

        The single source the arbiter derives contention from. `ordo serve` registers the
        preemptible primary-device residents from here instead of a `--resident-service`
        default, and compose renders an admission gate for each `enforcement: gate` service.
        """
        return gpu.inventory(self.hardware, self.plugin_services,
                             resident_vram_gb=self.resident_vram_gb())

    def manifest(self) -> dict[str, Any]:
        return {
            "hardware": self.hardware.summary(),
            "tier": self.tier,
            "model": {"id": self.model.id, "file": self.model.file, "vram_gb": self.model.vram_gb,
                      "resident_vram_gb": self.resident_vram_gb()},
            "ctx_size": self.ctx_size,
            # The declared GPU-contention map — what competes for which card and how it is
            # arbitrated. Surfaced in the manifest (and via ops-controller /status) so the
            # answer to "who can touch the GPU" is inspectable, not folklore.
            "gpu_arbitration": [c.as_dict() for c in self.gpu_inventory()],
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
        """The isolated, runnable compose for the stack — built from the resolved plugin
        services (data-driven), with the primary- AND secondary-GPU uuids resolved for the pins."""
        pri = self.hardware.primary_gpu
        sec = self.hardware.secondary_gpu
        return compose.render_compose(
            has_gpu=self.hardware.has_gpu, compose_profiles=self.compose_profiles,
            agent=self.hermes.get("agent", "hermes"), project=project,
            agent_image=self.hermes.get("agent_image") or None,
            agent_command=self.hermes.get("agent_command") or None,
            agent_user=self.hermes.get("agent_user") or None,
            agent_group_add=self.hermes.get("agent_group_add") or None,
            agent_volumes=self.hermes.get("agent_volumes") or None,
            agent_environment=self.hermes.get("agent_environment") or None,
            agent_secret_files=self.hermes.get("agent_secret_files") or None,
            agent_depends_on=self.hermes.get("agent_depends_on") or None,
            agent_healthcheck=self.hermes.get("agent_healthcheck") or None,
            dashboard=self.dashboard,
            llamacpp_image=self.env.get("LLAMACPP_IMAGE") or None,
            plugin_services=self.plugin_services,
            primary_gpu_uuid=(pri.uuid if pri else None),
            secondary_gpu_uuid=(sec.uuid if sec else None),
            gpu_claims={c.service: c for c in self.gpu_inventory()},
            mcp_servers=self.mcp_servers)

    def write(self, out_dir: str | Path) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        # .env (derived — regenerated every time; hand-edits here do not survive)
        env_lines = ["# GENERATED by ordo render — do not hand-edit; change ordo.yaml instead"]
        env_lines += [f"{k}={v}" for k, v in sorted(self.env.items())]
        (out / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        (out / "hermes.context.json").write_text(json.dumps(self.hermes, indent=2), encoding="utf-8")
        (out / "manifest.json").write_text(json.dumps(self.manifest(), indent=2), encoding="utf-8")
        # services-catalog.json — the dashboard's service-card catalog, aggregated from the
        # per-service `services/<id>/catalog.json` fragments (each service declares its own
        # card(s) next to its other manifests). Mounted read-only into the dashboard like the
        # manifest; services_catalog.py loads it (SERVICES_CATALOG_PATH) and React renders it.
        (out / "services-catalog.json").write_text(
            json.dumps(aggregate_services_catalog(), indent=2) + "\n", encoding="utf-8")
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
        # model-gateway/ - mounted read-only into model-gateway + model-gateway-keys at /config.
        mg_dir = out / "model-gateway"
        mg_dir.mkdir(parents=True, exist_ok=True)
        (mg_dir / "keys.json").write_text(
            json.dumps({"keys": self.litellm_keys}, indent=2) + "\n", encoding="utf-8")
        (mg_dir / "mcp_servers.yaml").write_text(
            render_litellm_mcp_fragment(self.mcp_servers), encoding="utf-8")
        # mcp/servers.json: the dashboard's read-only view (enabled servers + server_id->plugin_id map
        # for the enable/disable toggle that edits ordo.yaml). Mounted at /mcp-config.
        mcp_dir = out / "mcp"
        mcp_dir.mkdir(parents=True, exist_ok=True)
        (mcp_dir / "servers.json").write_text(json.dumps({
            "servers": [{k: s[k] for k in ("id", "litellm_name", "plugin_id", "name", "service", "url",
                                           "network", "tools", "hosted")}
                        for s in self.mcp_servers],
            "plugin_map": self.mcp_server_plugin_map,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # an isolated, runnable compose for the stack (own project/network, no port clashes)
        (out / "docker-compose.yml").write_text(
            yaml.safe_dump(self.compose_dict(), sort_keys=False),
            encoding="utf-8")


def aggregate_services_catalog(services_dir: str | Path = DEFAULT_PLUGINS_DIR) -> dict[str, Any]:
    """Aggregate the per-service dashboard-card fragments (services/<id>/catalog.json) into
    the single catalog document the dashboard consumes (out/services-catalog.json).

    Each fragment declares {"cards": [...]}; cards carry an explicit `order` so the curated
    grid order is deterministic regardless of glob order (id is the tiebreak). The dashboard's
    services_catalog._load_catalog_cards() applies the SAME ordering to raw fragments in its
    in-repo dev/test path — tests/test_services_catalog_fragments locks the two together.
    """
    cards: list[dict[str, Any]] = []
    for frag in sorted(Path(services_dir).glob("*/catalog.json")):
        data = json.loads(frag.read_text(encoding="utf-8"))
        cards.extend(dict(c) for c in (data.get("cards") or []))
    cards.sort(key=lambda c: (int(c.get("order", 1000)), str(c.get("id", ""))))
    return {"version": 1, "services": cards}


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
        "agent_group_add": (list(agent.group_add) if agent else []),
        "agent_volumes": (list(agent.volumes) if agent else []),
        "agent_environment": (dict(agent.environment) if agent else {}),
        "agent_secret_files": ([dict(s) for s in agent.secret_files] if agent else []),
        "agent_depends_on": (dict(agent.depends_on) if agent else {}),
        "agent_healthcheck": (dict(agent.healthcheck) if agent else {}),
    }
    model_gateway = {"ctx": ctx, "model_id": "local-chat"}

    # Resolve the chosen control-plane UI from the registry (native is the default). Unknown id ->
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
    # server_id → plugin_id for EVERY registered kind=mcp plugin, not just the enabled ones. The
    # server_id defaults to the plugin id (mirrors _render_mcp), decoupled only when a plugin sets
    # mcp.server_id (e.g. plugin `comfyui-mcp` → server `comfyui`). Lets the dashboard re-enable a
    # currently-disabled MCP by mapping its server id to the plugin to add back to ordo.yaml.
    mcp_server_plugin_map = {p.mcp.server_id: p.id for p in plugins.plugins if p.kind == "mcp" and p.mcp}

    # Per-consumer LiteLLM keys: the agent first, then every enabled plugin that declares one.
    key_consumers: list[tuple[str, dict[str, Any]]] = []
    if agent is not None and agent.litellm_key:
        key_consumers.append((agent.id, dict(agent.litellm_key)))
    key_consumers += [(p.id, dict(p.litellm_key)) for p in enabled if p.litellm_key]
    litellm_keys = render_litellm_keys(key_consumers, [s["id"] for s in mcp_servers])

    # Internal base URLs for gate-enforced services. A gate is a drop-in on the upstream's port,
    # so redirecting every in-stack consumer through it is a hostname change — but it must be ONE
    # change, in one place, or half the callers keep bypassing arbitration. The derived value
    # lands in .env and every consumer manifest reads `${<VAR>:-http://<service>:<port>}`, so the
    # fallback is the direct URL if the service ever stops being gated. The var NAME is a fact
    # about the existing consumers (they already read COMFYUI_URL), which is why it is a small
    # explicit table rather than something derived.
    for _p, _ps in plugin_services:
        arb = _ps.gpu_arbitration
        var = GATED_SERVICE_URL_ENV.get(_ps.name)
        if arb is not None and arb.enforcement == "gate" and var:
            env[var] = f"http://{gpu.gate_service_name(_ps.name)}:{arb.gate.listen_port}"

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
    for k in litellm_keys:
        if k["env"] not in required_secrets:
            required_secrets.append(k["env"])
    # Each MCP server's bearer token: LiteLLM presents it as `os.environ/<auth_secret>`, so the key
    # must be in secrets.env.example or the gateway dials that server with an empty credential.
    for s in mcp_servers:
        if s["auth_secret"] and s["auth_secret"] not in required_secrets:
            required_secrets.append(s["auth_secret"])

    return RenderedConfig(
        hardware=hw, model=model, ctx_size=ctx, tier=(model.tier),
        warnings=warnings + mcp_notes, env=env, hermes=hermes, model_gateway=model_gateway,
        dashboard=dashboard,
        plugins_enabled=[p.id for p in services], compose_profiles=compose_profiles,
        mcp_servers=mcp_servers, mcp_server_plugin_map=mcp_server_plugin_map,
        plugin_services=plugin_services,
        required_secrets=required_secrets,
        litellm_keys=litellm_keys,
    )


def _is_project_image(image: str, project: str = "ordo") -> bool:
    """A locally-BUILT project MCP image (e.g. ordo/qdrant-rag-mcp:latest). It has no public
    registry to digest-pin against — it's pinned by its build context (like llamacpp-patched), so
    it's reproducible without an @sha256. Preflight surfaces it as 'build first', not a leak risk."""
    return image.startswith(f"{project}/")


def render_litellm_mcp_fragment(servers: list[dict[str, Any]]) -> str:
    """The `mcp_servers:` map LiteLLM loads (merged into the proxy config by the model-gateway
    entrypoint). `transport` and `available_on_public_internet` are ALWAYS explicit: LiteLLM's
    documented defaults (sse / false) disagree with its code (http / true)."""
    entries: dict[str, Any] = {}
    for s in servers:
        # LiteLLM validates the server NAME (no MCP_TOOL_PREFIX_SEPARATOR, default `-`, because it
        # prefixes tools as `<name>-<tool>`), so the fragment is keyed by litellm_name throughout.
        # The hyphenated server_id survives in the URL (the compose service is `mcp-<server_id>`).
        name = s["litellm_name"]
        entry: dict[str, Any] = {
            "server_id": name,
            "url": s["url"],
            "transport": "http",
            "description": s["description"] or s["name"],
            "timeout": s["timeout"],
            "available_on_public_internet": False,
            "mcp_info": {"server_name": name},
        }
        if s["auth_type"]:
            entry["auth_type"] = s["auth_type"]
            entry["auth_value"] = f"os.environ/{s['auth_secret']}"
        if s["allowed_tools"]:
            entry["allowed_tools"] = list(s["allowed_tools"])
        entries[name] = entry
    header = (
        "# GENERATED by ordo render: the LiteLLM `mcp_servers` fragment, merged into the proxy config by\n"
        "# services/model-gateway/entrypoint.sh. Do not hand-edit (change services/*/plugin.yaml).\n"
    )
    return header + yaml.safe_dump({"mcp_servers": entries}, sort_keys=False)


def _render_mcp(mcps: list, project: str = "ordo") -> tuple[list[dict[str, Any]], list[str]]:
    """Build the MCP server records (one per enabled kind=mcp plugin) that compose and the LiteLLM
    fragment render from. Public images MUST be digest-pinned (no Docker online-catalog roulette,
    the leak/drift source V1 suffered). Locally-built project images (ordo/*) are exempt: they are
    pinned by build context, not a registry digest. A hosted server declares a `url:` and no image,
    so there is nothing to pin and nothing to build."""
    servers: list[dict[str, Any]] = []
    notes: list[str] = []
    seen_ids: dict[str, str] = {}
    seen_litellm_names: dict[str, str] = {}
    for p in mcps:
        spec = p.mcp
        if spec is None:
            continue
        image = spec.image
        digest = image.split("@sha256:")[-1] if "@sha256:" in image else ""
        if spec.hosted or _is_project_image(image, project):
            pass  # hosted (no image) or locally built (pinned by build context)
        elif not digest:
            notes.append(f"mcp '{p.id}': image is not digest-pinned - refuse in production")
        elif len(set(digest)) <= 1:  # placeholder like 000.../111...
            notes.append(f"mcp '{p.id}': image digest is a placeholder - set the real sha256")
        if spec.server_id in seen_ids:
            notes.append(f"mcp '{p.id}': server_id '{spec.server_id}' collides with plugin '{seen_ids[spec.server_id]}'")
        seen_ids[spec.server_id] = p.id
        # Two distinct server_ids can still map to ONE LiteLLM name (`a-b` and `a_b`), which would
        # silently drop a server from the fragment map. Flag it rather than render the collision.
        if spec.litellm_name in seen_litellm_names:
            notes.append(f"mcp '{p.id}': litellm name '{spec.litellm_name}' collides with plugin "
                         f"'{seen_litellm_names[spec.litellm_name]}'")
        seen_litellm_names[spec.litellm_name] = p.id
        servers.append({
            "id": spec.server_id, "litellm_name": spec.litellm_name,
            "plugin_id": p.id, "name": p.name, "description": p.description,
            "image": image, "hosted": spec.hosted, "url": spec.internal_url(),
            "service": spec.service_name, "port": spec.port, "path": spec.path, "network": spec.network,
            "command": list(spec.command), "env": dict(spec.env), "volumes": list(spec.volumes),
            "depends_on": list(spec.depends_on), "timeout": spec.timeout,
            "auth_type": spec.auth_type, "auth_secret": spec.auth_secret,
            "allowed_tools": list(spec.allowed_tools), "tools": list(spec.tools),
            "healthcheck": dict(spec.healthcheck),
        })
    return servers, notes
