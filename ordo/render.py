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

from . import compose, gpu, images, substrate
from .agents import AgentRegistry
from .catalog import DEFAULT_VRAM_RESERVE_GB, Catalog, Model
from .config import Source
from .dashboards import DashboardRegistry
from .hardware import HardwareProfile, detect
from .llamacpp_backend import CPU as CPU_BACKEND
from .llamacpp_backend import LlamaCppBackend
from .llamacpp_backend import select as select_backend
from .plugins import LOOPBACK, Plugin, PluginRegistry

# Render data now lives co-located under services/<id>/ (plugin.yaml / agent.yaml / dashboard.yaml
# / catalog.json); each registry globs its own manifest kind out of the shared services/ root.
DEFAULT_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "services"
DEFAULT_AGENTS_DIR = Path(__file__).resolve().parent.parent / "services"
DEFAULT_DASHBOARDS_DIR = Path(__file__).resolve().parent.parent / "services"

# The LiteLLM proxy config the model-gateway image bakes in. Its `model_list` is the ONE place
# model names exist, so a `litellm_key.models` grant is validated against it rather than a copy.
LITELLM_CONFIG_TEMPLATE = Path(__file__).resolve().parent.parent / "services" / "model-gateway" / "litellm_config.yaml"

# Gate-enforced service -> the .env key its in-stack consumers already use for its base URL.
# When the service is gated, render points that key at the gate so mcp-comfyui, the
# dashboard and the control plane all submit through arbitration instead of around it. Guarded by
# tests/substrate/test_gpu_arbitration.py (every gated service must appear here, or its consumers
# would silently keep the direct route).
GATED_SERVICE_URL_ENV: dict[str, str] = {"comfyui": "COMFYUI_URL"}

# Langfuse's edge port (auth/caddy/Caddyfile's `:8450` site, published by the edge plugin) and the
# clean tailnet label its sidecar registers. Both are facts about the edge wiring, kept here so the
# derived LANGFUSE_PUBLIC_URL below cannot disagree with the Caddyfile / sidecar that serve it.
LANGFUSE_EDGE_PORT = 8450
LANGFUSE_TAILNET_LABEL = "langfuse"
# Fallback login identity for the headless Langfuse init. A site `LANGFUSE_ADMIN_EMAIL` overrides it.
LANGFUSE_DEFAULT_ADMIN_EMAIL = "admin@ordo.local"

# model-gateway's edge port (Caddyfile's `:8449` site) and tailnet sidecar label - the same two
# facts LANGFUSE_EDGE_PORT/LANGFUSE_TAILNET_LABEL record for Langfuse, sourced from
# services/model-gateway/catalog.json's `sso_port`/`tailnet_label` (the dashboard Open link uses
# the same two values via services_catalog.service_open_url()).
LITELLM_EDGE_PORT = 8449
LITELLM_TAILNET_LABEL = "llm"

# Secret env KEYS the CORE services need at runtime (values operator-managed in secrets.env, never
# rendered). model-gateway/model-gateway-keys/ops-controller/dashboard/agent read these; plugins add more
# via their manifest `secrets:` list. Mirrors the V1 SOPS-decrypted runtime/.env surface.
CORE_SECRET_KEYS: tuple[str, ...] = (
    "LITELLM_MASTER_KEY",         # model-gateway master key (LiteLLM admin + UI login)
    "LITELLM_SALT_KEY",           # LiteLLM DB credential-encryption salt. NEVER rotate (stored creds unreadable)
    "LITELLM_DB_PASSWORD",        # litellm-db postgres password (compose-interpolated into DATABASE_URL)
    "OPS_CONTROLLER_TOKEN",       # bearer between agent/dashboard/mcp <-> ops-controller
    # NB: no DASHBOARD_AUTH_TOKEN. Operators reach the dashboard through the Caddy edge SSO;
    # internal callers of its protected routes send OPS_CONTROLLER_TOKEN (dashboard/auth.py).
    # Without the edge, the dashboard manifest's `local_login_secret` is added by render() below.
    # NB: THROUGHPUT_RECORD_TOKEN is intentionally NOT required. There is no SOPS source that can
    # supply it, and the dashboard only enforces it "when set" (dashboard/app.py) — the /api/
    # throughput/record route is open when the var is empty. Demanding a key nothing can provide
    # would make secrets.env.example (and any preflight secrets-completeness check) list an
    # unfulfillable key. It stays an OPTIONAL var: set it to harden the internal route, or leave
    # it unset. (2026-07-24 hardening audit.)
    "HF_TOKEN",                   # Hugging Face (gated model pulls)
    "GITHUB_PERSONAL_ACCESS_TOKEN",  # ComfyUI-Manager (git-based node installs)
)

# The core keys above the stack runs WITHOUT (they only unlock gated downloads). Listed so a blank
# one is a preflight note, not a blocker; a plugin that reads one declares it in `optional_secrets:`.
CORE_OPTIONAL_SECRET_KEYS: tuple[str, ...] = ("HF_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")

# Secrets a service may read that are deliberately NOT required (not in secrets.env.example):
# THROUGHPUT_RECORD_TOKEN has no SOPS source and the dashboard only enforces it "when set" (see the
# note in CORE_SECRET_KEYS). A service passes these as ${KEY:-} so an absent value is simply empty.
OPTIONAL_SECRET_KEYS: tuple[str, ...] = ("THROUGHPUT_RECORD_TOKEN",)

# The SSO edge plugin (services/edge). Whether it is enabled is THE switch between the two access
# modes; nothing else (no flag, no env var) selects the mode.
EDGE_PLUGIN = "edge"


def local_access(enabled_plugin_ids) -> bool:
    """True when the render has no edge: UIs publish loopback ports and the dashboard takes a local
    sign-in. False when the edge is Caddy's single front door with SSO."""
    return EDGE_PLUGIN not in enabled_plugin_ids


# Deep-merge an override dict onto a derived dict (overrides win, survive regeneration).
def _apply_overrides(derived: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    out = dict(derived)
    for k, v in (overrides or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _apply_overrides(out[k], v)
        else:
            out[k] = v
    return out


# Required keys for `cost:` (ordo.yaml): all four or none. Missing/non-positive/unknown ->
# ValueError naming the offending key, caught at render time rather than silently defaulting.
_COST_KEYS = ("usd_per_kwh", "inference_watts", "prompt_tokens_per_second", "output_tokens_per_second")


def _format_cost(value: float) -> str:
    """Plain decimal string (never scientific notation, never a trailing bare dot).

    LiteLLM/YAML both parse `1.51e-08` fine, but a plain decimal is unambiguous everywhere
    it's read (the .env file, `docker inspect`, a human diffing the render): so always emit
    fixed-point, trimmed of the trailing zeros a fixed precision leaves behind.
    """
    text = format(value, ".18f").rstrip("0")
    if text.endswith("."):
        text += "0"
    return text


def local_token_costs(cost: dict[str, Any]) -> tuple[str, str]:
    """Electricity-derived (input_cost_per_token, output_cost_per_token) as decimal strings.

    Empty `cost` -> ("0", "0") (the historical $0/token default). Otherwise all four keys in
    `_COST_KEYS` are required and must be positive numbers:

        usd_per_second = inference_watts / 1000 * usd_per_kwh / 3600
        input_cost_per_token  = usd_per_second / prompt_tokens_per_second
        output_cost_per_token = usd_per_second / output_tokens_per_second

    i.e. the rig's running cost in dollars-per-second, divided by however many tokens/sec it
    produces at that draw. A missing/non-positive/unrecognized key raises ValueError naming it.
    """
    if not cost:
        return ("0", "0")
    unknown = sorted(set(cost) - set(_COST_KEYS))
    if unknown:
        raise ValueError(f"cost: unknown key(s) {unknown!r}, expected only {list(_COST_KEYS)!r}")
    values: dict[str, float] = {}
    for key in _COST_KEYS:
        if key not in cost:
            raise ValueError(f"cost.{key} is required when cost: is set")
        raw = cost[key]
        if isinstance(raw, bool) or not isinstance(raw, int | float) or raw <= 0:
            raise ValueError(f"cost.{key} must be a positive number, got {raw!r}")
        values[key] = float(raw)
    usd_per_second = values["inference_watts"] / 1000 * values["usd_per_kwh"] / 3600
    input_cost = usd_per_second / values["prompt_tokens_per_second"]
    output_cost = usd_per_second / values["output_tokens_per_second"]
    return (_format_cost(input_cost), _format_cost(output_cost))


def key_env_name(consumer_id: str) -> str:
    """`open-webui` -> `LITELLM_KEY_OPEN_WEBUI` (the secrets.env var carrying that consumer's key)."""
    return "LITELLM_KEY_" + re.sub(r"[^A-Za-z0-9]", "_", consumer_id).upper()


def litellm_model_names(config_path: Path | None = None) -> list[str]:
    """The model names `services/model-gateway/litellm_config.yaml` puts in LiteLLM's `model_list`.

    Only the STABLE names are grantable. `__GPU_MODEL_NAME__` / `__CPU_MODEL_NAME__` are entrypoint
    placeholders substituted from the deployed GGUF filenames at container start, so their value is
    a deployment fact the render cannot know; a key may not be pinned to one (it would break on the
    next model swap). Parsed from the config rather than copied, so the list cannot drift from it.
    """
    path = config_path or LITELLM_CONFIG_TEMPLATE
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    names = [str(m.get("model_name", "")) for m in (config.get("model_list") or [])]
    return sorted(n for n in names if n and not n.startswith("__"))


def render_litellm_keys(consumers: list[tuple[str, dict[str, Any]]],
                        server_names: list[str],
                        model_names: list[str] | None = None) -> list[dict[str, Any]]:
    """Turn each consumer's `litellm_key:` declaration into a key grant for bootstrap_keys.py.

    The grant names servers by their LITELLM name (McpSpec.litellm_name: hyphen-free), because
    LiteLLM expands an `object_permission.mcp_servers` entry by an exact server_id/alias/
    server_name match and passes an unmatched entry straight through to a deny. A hyphenated
    entry would therefore revoke that server from the key in silence.

    `mcp_servers: all` expands to every ENABLED server name (sorted); a list may name only enabled
    servers, else the render fails (a typo must not silently grant nothing).

    `models:` is validated the same way and is fail-closed: LiteLLM reads an EMPTY `models` list as
    access to every model, so an absent or empty list is a privilege escalation, not an empty
    grant, and every name must be one the gateway actually serves (litellm_model_names)."""
    keys: list[dict[str, Any]] = []
    known = sorted(server_names)
    known_models = sorted(model_names if model_names is not None else litellm_model_names())
    for consumer_id, spec in consumers:
        if not spec:
            continue
        models = [str(m) for m in (spec.get("models") or [])]
        if not models:
            raise ValueError(f"litellm_key for '{consumer_id}' declares no models; LiteLLM reads an "
                             f"empty list as access to EVERY model, so name them explicitly "
                             f"(available: {known_models})")
        unknown_models = [m for m in models if m not in known_models]
        if unknown_models:
            raise ValueError(f"litellm_key for '{consumer_id}' grants unknown models: {unknown_models} "
                             f"(the gateway serves {known_models})")
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


def _max_ctx_for_vram(model: Model, hw: HardwareProfile, reserve_gb: float,
                      backend: LlamaCppBackend) -> int:
    """Largest context that fits after weights + reserve, capped at the model's trained ctx.

    Encodes the KV-math lesson: KV grows ~linearly with ctx, so on a smaller card we cut
    ctx rather than spill. Falls back to ctx_default when we can't estimate (CPU / no kv rate).
    """
    if not backend.accelerated or not model.kv_kb_per_token:
        return model.ctx_default
    free_after_weights_gb = hw.primary_vram_gb - model.vram_gb - reserve_gb
    if free_after_weights_gb <= 0:
        return min(model.ctx_default, 8192)
    max_tokens = int((free_after_weights_gb * 1024 * 1024) / model.kv_kb_per_token)
    # round down to a tidy multiple of 8K
    max_tokens = (max_tokens // 8192) * 8192
    return max(8192, min(model.ctx_default, max_tokens))


# Files older renders emitted into out/ and this one no longer does: the Docker mcp-gateway
# artefacts retired by #185. write() deletes them, because out/mcp is mounted into the dashboard
# and a stale file there looks live. An explicit list, not "everything write() did not emit":
# out/ also holds operator-owned state (ordo.yaml, secrets.env, lease history, certs).
RETIRED_OUTPUTS = (
    "mcp-registry.yaml",
    "mcp/servers.txt",
    "mcp/registry-custom.yaml",
    "mcp/registry-custom.docker.yaml",
    "mcp/server-plugin-map.json",
)


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
    # dashboard service's image/env/depends/healthcheck.
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
    # The subset of required_secrets the stack runs without: every enabled plugin that reads the
    # key declares it `optional_secrets:`. Preflight notes a blank one instead of blocking.
    optional_secrets: list[str] = dataclasses.field(default_factory=list)
    # Per-consumer LiteLLM virtual-key grants declared by the agent/plugin manifests
    # (`litellm_key:`). Rendered to out/model-gateway/keys.json, which the model-gateway-keys
    # one-shot reads to provision the keys against the running LiteLLM proxy.
    litellm_keys: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # The llama.cpp build this host runs (ordo/llamacpp_backend.py). Its image is the default for
    # the chat service (a model's `backend_image` or an override still wins, via LLAMACPP_IMAGE);
    # its device wiring is what compose gives that service.
    llamacpp_backend: LlamaCppBackend = CPU_BACKEND
    # The first-party images (`ordo/<name>`, untagged) whose tag render fills in from the
    # out/images.json record `ordo build` writes. See ordo/images.py.
    first_party_images: tuple[str, ...] = ()

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
                      "resident_vram_gb": self.resident_vram_gb(),
                      # Weights on disk ~ weights in VRAM; a CPU model declares RAM instead.
                      "disk_gb": self.model.vram_gb or self.model.ram_gb},
            "ctx_size": self.ctx_size,
            "llamacpp_backend": self.llamacpp_backend.name,
            # The declared GPU-contention map — what competes for which card and how it is
            # arbitrated. Surfaced in the manifest (and via ops-controller /status) so the
            # answer to "who can touch the GPU" is inspectable, not folklore.
            "gpu_arbitration": [c.as_dict() for c in self.gpu_inventory()],
            "plugins_enabled": self.plugins_enabled,
            "compose_profiles": self.compose_profiles,
            "mcp_servers": [s["id"] for s in self.mcp_servers],
            # Secret NAMES (never values): what `ordo up`'s preflight checks secrets.env against.
            "required_secrets": self.required_secrets,
            "optional_secrets": self.optional_secrets,
            "warnings": self.warnings,
            **self._dashboard_sign_in(),
            # What this render was made from. ops-controller refuses to re-render over a render made
            # from different inputs, so its baked copy cannot silently revert the checkout's changes.
            "substrate_digest": substrate.current_digest(),
            "derived": {
                "env.LLAMACPP_CTX_SIZE": self.env["LLAMACPP_CTX_SIZE"],
                "env.LLAMACPP_CPU_CTX": self.env["LLAMACPP_CPU_CTX"],
                "hermes.context_length": self.hermes["context_length"],
                "model_gateway.ctx": self.model_gateway["ctx"],
            },
        }

    def _dashboard_sign_in(self) -> dict[str, Any]:
        """`dashboard_sign_in: {url, secret}` while the dashboard is published on loopback with a
        local sign-in secret (the edge is off); nothing with the edge on. `ordo up` reads it to
        mint the secret and print the sign-in link, so neither the port nor the key name is repeated."""
        local_port = self.dashboard.get("local_port")
        secret = self.dashboard.get("local_login_secret")
        if not (local_access(self.plugins_enabled) and local_port is not None and secret):
            return {}
        return {"dashboard_sign_in": {"url": f"http://{LOOPBACK}:{local_port.host}", "secret": secret}}

    def compose_dict(self, project: str = "ordo", image_tags: dict[str, str] | None = None) -> dict[str, Any]:
        """The isolated, runnable compose for the stack — built from the resolved plugin
        services (data-driven), with the primary- AND secondary-GPU uuids resolved for the pins.
        Only NVIDIA cards are pinned or reserved: `driver: nvidia` is the only GPU device driver
        compose has, and a request for it on a host without it stops the container starting.

        `image_tags` is the `ordo build` record ({image: tag}); a first-party image it does not
        name renders at `images.FALLBACK_TAG`."""
        nvidia = self.hardware.primary_is_nvidia
        pri = self.hardware.primary_gpu if nvidia else None
        sec = self.hardware.secondary_gpu if nvidia else None
        if sec is not None and sec.vendor != "nvidia":
            sec = None
        doc = compose.render_compose(
            nvidia_gpu=nvidia, compose_profiles=self.compose_profiles,
            agent=self.hermes.get("agent", "hermes"), project=project,
            agent_image=self.hermes.get("agent_image") or None,
            agent_command=self.hermes.get("agent_command") or None,
            agent_user=self.hermes.get("agent_user") or None,
            agent_group_add=self.hermes.get("agent_group_add") or None,
            agent_volumes=self.hermes.get("agent_volumes") or None,
            agent_environment=self.hermes.get("agent_environment") or None,
            agent_secret_files=self.hermes.get("agent_secret_files") or None,
            agent_secrets=self.hermes.get("agent_secrets") or None,
            agent_derived_env=self.hermes.get("agent_derived_env") or None,
            litellm_key_envs=[k["env"] for k in self.litellm_keys],
            agent_depends_on=self.hermes.get("agent_depends_on") or None,
            agent_healthcheck=self.hermes.get("agent_healthcheck") or None,
            dashboard=self.dashboard,
            llamacpp_backend=self.llamacpp_backend,
            llamacpp_image=self.env["LLAMACPP_IMAGE"],
            plugin_services=self.plugin_services,
            primary_gpu_uuid=(pri.uuid if pri else None),
            secondary_gpu_uuid=(sec.uuid if sec else None),
            gpu_claims={c.service: c for c in self.gpu_inventory()},
            mcp_servers=self.mcp_servers,
            # Gateway-wide Langfuse tracing follows the plugin: on with it, absent without it.
            langfuse_tracing="langfuse" in self.plugins_enabled,
            # With the edge on, Caddy is the one front door; without it, each UI that declares a
            # `local_port` publishes it on loopback so a local-only install can reach it.
            publish_local_ports=local_access(self.plugins_enabled),
            # {} when the edge wiring can't produce a PROXY_BASE_URL (see litellm_google_sso_env).
            litellm_google_sso_env=self.model_gateway.get("google_sso_env") or {},
            # The keys this render wrote to .env: a service's declared derived key renders as a
            # `${KEY?}` reference only when the render produced it.
            available_env=frozenset(self.env))
        images.pin_first_party(doc["services"], self.first_party_images, image_tags or {})
        return doc

    def write(self, out_dir: str | Path) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        # .env (derived — regenerated every time; hand-edits here do not survive)
        env_lines = ["# GENERATED by ordo render — do not hand-edit; change ordo.yaml instead"]
        env_lines += [f"{k}={v}" for k, v in sorted(self.env.items())]
        (out / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
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
            "# Copy to `secrets.env` and fill real values (never commit secrets.env). Each service",
            "# gets only the keys it declares, interpolated by compose from `--env-file secrets.env`.",
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
        # an isolated, runnable compose for the stack (own project/network, no port clashes). The
        # first-party image tags come from the record `ordo build` keeps in this same directory, so
        # the host render and ops-controller's render (out/ is its /config) pin the same builds.
        (out / "docker-compose.yml").write_text(
            yaml.safe_dump(self.compose_dict(image_tags=images.load_record(out)), sort_keys=False),
            encoding="utf-8")
        for retired in RETIRED_OUTPUTS:
            (out / retired).unlink(missing_ok=True)


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


def langfuse_public_url(env: dict[str, str], plugins_enabled: list[str]) -> str:
    """The browser-facing origin Langfuse must be told about (it becomes NEXTAUTH_URL).

    Langfuse builds its sign-in redirect and every emitted link from this value, so it has to be
    the URL the browser actually used - a mismatch lands the user on an unreachable host after
    Google SSO. That URL is entirely determined by the edge layer already rendered here, so it is
    derived rather than hand-set, exactly like the rest of the edge wiring:

      tailnet-names enabled -> the clean sidecar name   https://langfuse.<CADDY_TAILNET_DOMAIN>
      edge enabled          -> the SSO-gated port root  https://<CADDY_TAILNET_HOSTNAME>:8450
      neither               -> "" (a local install)

    BOTH branches are gated on the plugin that actually serves the URL, not merely on the
    hostname being set. `:8450` exists only because the edge plugin publishes it and the
    Caddyfile has a site for it; a stack that sets CADDY_TAILNET_HOSTNAME with the edge
    disabled would otherwise be handed a port nothing listens on. Same reason the sidecar
    branch checks `tailnet-names` rather than just the domain.

    Empty is a valid answer, not a failure: Langfuse boots and serves the API with an empty
    NEXTAUTH_URL; only the interactive browser sign-in needs the origin to match.
    """
    domain = str(env.get("CADDY_TAILNET_DOMAIN", "") or "").strip()
    hostname = str(env.get("CADDY_TAILNET_HOSTNAME", "") or "").strip()
    if "tailnet-names" in plugins_enabled and domain:
        return f"https://{LANGFUSE_TAILNET_LABEL}.{domain}"
    if "edge" in plugins_enabled and hostname:
        return f"https://{hostname}:{LANGFUSE_EDGE_PORT}"
    return ""


def litellm_google_sso_env(env: dict[str, str], plugins_enabled: list[str], admin_identity: str) -> dict[str, str]:
    """model-gateway's LiteLLM admin UI Google SSO wiring, or {} when it can't be derived.

    Lets the operator sign into the LiteLLM admin UI with the SAME Google identity the edge
    already gates, instead of a second admin/LITELLM_MASTER_KEY login. Reuses the stack's
    existing Google OAuth client - GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are compose-level
    `${...}` references to the edge's own OAUTH2_PROXY_CLIENT_ID/OAUTH2_PROXY_CLIENT_SECRET
    (resolved from secrets.env at `docker compose` time, same as CADDY_TAILNET_HOSTNAME below),
    so this introduces no new secret and this function never handles a secret value.

    PROXY_BASE_URL must be the ORIGIN the browser actually used: LiteLLM appends
    "/sso/callback" to it verbatim, both to build the redirect it sends Google and as the value
    the operator must register as an authorized redirect URI in Google Cloud Console
    (litellm/proxy/management_endpoints/ui_sso.py: get_redirect_url_for_sso() ->
    proxy/utils.py:get_custom_url(), which prefers PROXY_BASE_URL over the request's own host).
    Derived exactly like LANGFUSE_PUBLIC_URL above, from the same edge wiring already rendered
    here, and gated the same way - BOTH branches require the edge plugin (tailnet-names
    `depends_on: [edge]`, so it can't be enabled without edge):

      tailnet-names enabled -> the clean sidecar name   https://llm.<CADDY_TAILNET_DOMAIN>
      edge enabled          -> the SSO-gated port root  https://<CADDY_TAILNET_HOSTNAME>:8449
      neither               -> {} (no SSO vars at all; the admin/master-key login is unaffected)

    PROXY_ADMIN_ID is included only when `admin_identity` (the site `LITELLM_ADMIN_IDENTITY`
    key) is set: LiteLLM promotes the SSO user whose id equals PROXY_ADMIN_ID to proxy_admin
    (check_and_update_if_proxy_admin_id). For Google SSO that id is the Google account's OpenID
    `sub` (fastapi_sso.sso.google.GoogleSSO), NOT the email address - see
    services/model-gateway/README.md for how an operator finds it. Left unset, every Google
    sign-in lands as a view-only internal user and nobody is auto-promoted.
    """
    domain = str(env.get("CADDY_TAILNET_DOMAIN", "") or "").strip()
    hostname = str(env.get("CADDY_TAILNET_HOSTNAME", "") or "").strip()
    base_url = ""
    if "tailnet-names" in plugins_enabled and domain:
        base_url = f"https://{LITELLM_TAILNET_LABEL}.{domain}"
    elif "edge" in plugins_enabled and hostname:
        base_url = f"https://{hostname}:{LITELLM_EDGE_PORT}"
    if not base_url:
        return {}
    sso_env = {
        "PROXY_BASE_URL": base_url,
        "GOOGLE_CLIENT_ID": "${OAUTH2_PROXY_CLIENT_ID}",
        "GOOGLE_CLIENT_SECRET": "${OAUTH2_PROXY_CLIENT_SECRET}",
    }
    if admin_identity:
        sso_env["PROXY_ADMIN_ID"] = admin_identity
    return sso_env


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
    backend, backend_notes = select_backend(hw)
    model, warnings = catalog.resolve(hw, source.model, source.tier, reserve_gb)
    warnings = backend_notes + warnings
    if plugins is None:
        plugins = PluginRegistry.load(DEFAULT_PLUGINS_DIR)
    if agents is None:
        agents = AgentRegistry.load(DEFAULT_AGENTS_DIR)
    if dashboards is None:
        dashboards = DashboardRegistry.load(DEFAULT_DASHBOARDS_DIR)

    ctx = _max_ctx_for_vram(model, hw, reserve_gb, backend)

    # --- one source value → every consumer (this is the whole point) ---
    derived: dict[str, Any] = {
        "llamacpp": {
            "ctx_size": ctx,
            "model": model.file,
            "gpu_layers": -1 if backend.accelerated else 0,
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
            # The model's special build when it pins one, else this host's backend build.
            "image": model.backend_image or backend.image,
        },
    }
    # `overrides:` survive regeneration; everything else is recomputed each render.
    derived = _apply_overrides(derived, source.overrides)
    lc = derived["llamacpp"]
    ctx = int(lc["ctx_size"])  # re-read in case an override pinned it

    env = {
        "LLAMACPP_MODEL": str(lc["model"]),
        "LLAMACPP_CTX_SIZE": str(ctx),
        # The CPU failover window is the SAME resolved window, by construction. LiteLLM fails
        # `local-chat` over to llamacpp-cpu whenever the GPU is evicted, so a failover that
        # accepts less than the primary rejects requests exactly when it is needed. Sizing the
        # GPU window down for a heavier model used to leave the CPU side on its compose default,
        # and the two silently diverged. That default (`${LLAMACPP_CPU_CTX:-...}` in
        # services/llamacpp-cpu) now only applies to an .env this renderer never wrote.
        "LLAMACPP_CPU_CTX": str(ctx),
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
    # The image the chat service runs, always explicit: compose reads it from here, and
    # model-gateway advertises it (served_by), so both name the build that is actually running.
    env["LLAMACPP_IMAGE"] = str(lc["image"])
    # Electricity-derived per-token cost for every local model (local-chat, the GPU/CPU pins,
    # local-embed): see local_token_costs. Empty `cost:` -> "0"/"0" (unchanged $0 default).
    input_cost_per_token, output_cost_per_token = local_token_costs(source.cost)
    env["LOCAL_INPUT_COST_PER_TOKEN"] = input_cost_per_token
    env["LOCAL_OUTPUT_COST_PER_TOKEN"] = output_cost_per_token
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
        "agent_secrets": (list(agent.secrets) if agent else []),
        "agent_derived_env": (list(agent.derived_env) if agent else []),
        "agent_depends_on": (dict(agent.depends_on) if agent else {}),
        "agent_healthcheck": (dict(agent.healthcheck) if agent else {}),
    }
    model_gateway = {"ctx": ctx, "model_id": "local-chat"}

    # Resolve the chosen control-plane UI from the registry (native is the default). Unknown id ->
    # a warning + fall back to the default, so a typo surfaces at render/preflight. The selected
    # dashboard flows its image/env/depends/healthcheck into compose.
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
            "secrets": list(dash.secrets),
            "derived_env": list(dash.derived_env),
            "gpu_capabilities": list(dash.gpu_capabilities),
            "local_port": dash.local_port,
            "local_login_secret": dash.local_login_secret,
        }
    # Registry-driven plugin resolution: enable what's requested AND fits AND has its required
    # site keys AND has its deps.
    enabled, notes = plugins.resolve(source.plugins, hw, source.site)
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
    litellm_keys = render_litellm_keys(key_consumers, [s["litellm_name"] for s in mcp_servers])

    # Internal base URLs for gate-enforced services. A gate is a drop-in on the upstream's port,
    # so redirecting every in-stack consumer through it is a hostname change — but it must be ONE
    # change, in one place, or half the callers keep bypassing arbitration. The derived value
    # lands in .env and consumer manifests read `${<VAR>}` with NO direct fallback: the gated
    # service sits on a network only its gate joins (compose.gated_upstream_net), so a direct URL
    # could not work anyway. The var NAME is a fact about the existing consumers (they already
    # read COMFYUI_URL), which is why it is a small explicit table rather than something derived.
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

    # Langfuse's two non-secret settings. Emitted only when the plugin is enabled (a stack without
    # it gets no dead keys), and with setdefault AFTER the site merge - unlike the derived keys
    # above, these are DEFAULTS an operator may deliberately override from `site:` (a Langfuse
    # reached through a different front door, or a real admin address for the seeded login).
    if "langfuse" in [p.id for p in services]:
        env.setdefault("LANGFUSE_PUBLIC_URL", langfuse_public_url(env, [p.id for p in services]))
        env.setdefault("LANGFUSE_ADMIN_EMAIL", LANGFUSE_DEFAULT_ADMIN_EMAIL)

    # LiteLLM admin UI Google SSO: model-gateway is core (always rendered), so - unlike Langfuse -
    # this is gated on the edge wiring alone, not on a plugin of its own. `LITELLM_ADMIN_IDENTITY`
    # is a plain `site:` key (already copied into `env` by the loop above); read here under its
    # own name because the compose var it feeds, PROXY_ADMIN_ID, is not.
    admin_identity = str(env.get("LITELLM_ADMIN_IDENTITY", "") or "").strip()
    model_gateway["google_sso_env"] = litellm_google_sso_env(env, [p.id for p in services], admin_identity)

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
    # The dashboard's local sign-in secret, only while it is published on loopback (edge off).
    local_login_secret = dashboard.get("local_login_secret")
    if (local_login_secret and dashboard.get("local_port") is not None
            and local_access([p.id for p in enabled]) and local_login_secret not in required_secrets):
        required_secrets.append(local_login_secret)
    # A key is optional only when every reader says so: the core list for a core key, and each
    # enabled plugin that declares it.
    readers: dict[str, list[Plugin]] = {}
    for p in enabled:
        for key in p.secrets:
            readers.setdefault(key, []).append(p)

    def _is_optional(key: str) -> bool:
        if key in CORE_SECRET_KEYS and key not in CORE_OPTIONAL_SECRET_KEYS:
            return False
        plugin_readers = readers.get(key, [])
        if not plugin_readers:
            return key in CORE_OPTIONAL_SECRET_KEYS
        return all(key in p.optional_secrets for p in plugin_readers)

    optional_secrets = [key for key in required_secrets if _is_optional(key)]

    return RenderedConfig(
        hardware=hw, model=model, ctx_size=ctx, tier=(model.tier),
        warnings=warnings + mcp_notes, env=env, hermes=hermes, model_gateway=model_gateway,
        dashboard=dashboard,
        plugins_enabled=[p.id for p in services], compose_profiles=compose_profiles,
        mcp_servers=mcp_servers, mcp_server_plugin_map=mcp_server_plugin_map,
        plugin_services=plugin_services,
        required_secrets=required_secrets,
        optional_secrets=optional_secrets,
        litellm_keys=litellm_keys,
        llamacpp_backend=backend,
        first_party_images=tuple(sorted(images.first_party_contexts(plugins, agents, dashboards))),
    )


def _is_project_image(image: str, project: str = "ordo") -> bool:
    """A locally-BUILT project MCP image (e.g. ordo/qdrant-rag-mcp). It has no public
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
    seen_litellm_names: dict[str, tuple[str, str]] = {}   # litellm_name -> (plugin id, server_id)
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
        # Two distinct server_ids can still map to ONE LiteLLM name (`a-b` and `a_b`). The fragment
        # is a name-keyed map, so the second one would overwrite the first and a server would vanish
        # from the gateway with nothing to show for it. Refuse the render (the component doc
        # promises rejection), rather than emit a config that quietly serves fewer servers.
        # Two plugins sharing ONE server_id is the separate seen_ids case noted just above.
        other_plugin, other_id = seen_litellm_names.get(spec.litellm_name, ("", ""))
        if other_id and other_id != spec.server_id:
            raise ValueError(
                f"mcp server_id '{spec.server_id}' (plugin '{p.id}') and server_id '{other_id}' "
                f"(plugin '{other_plugin}') both render to the LiteLLM name '{spec.litellm_name}'; "
                "the fragment is keyed by that name, so one of the two servers would be dropped "
                "silently. Give one of them a distinct mcp.server_id.")
        seen_litellm_names[spec.litellm_name] = (p.id, spec.server_id)
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
