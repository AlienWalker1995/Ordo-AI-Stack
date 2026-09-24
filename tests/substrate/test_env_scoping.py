"""Each service receives only the derived config it reads.

The rendered out/.env reached 43 services through a shared `env_file: .env`. Every key was therefore a
dependency of every one of them: a render that changed one key (a model switch, a plugin's
`*_ENABLED` flag) changed all 43 config hashes, and the next `up` recreated the whole stack, GPU
residents included. Now `.env` is only the compose INTERPOLATION source (`--env-file`): a service
declares the derived NAMES it reads (`derived_env:` on a plugin service, agent or dashboard manifest,
or the core lists in ordo/compose.py) and the renderer passes exactly those as `KEY: ${KEY?...}`.

SPEC below is the contract, derived from what each process actually reads (entrypoints, service code,
third-party images, the agent's skills and cron scripts). Giving a service another derived key means
changing SPEC on purpose.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ordo import compose as compose_mod
from ordo.agents import Agent, AgentRegistry
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.dashboards import Dashboard, DashboardRegistry
from ordo.plugins import PluginRegistry, PluginService
from ordo.render import GATED_SERVICE_URL_ENV, OPTIONAL_SECRET_KEYS, render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
# The site keys the edge and memory-vault plugins require, plus every optional `site:` knob a service
# reads at runtime, so every derived key a service can read is present in this render.
SITE = {"CADDY_BIND": "127.0.0.1", "CADDY_TAILNET_HOSTNAME": "host.example.ts.net",
        "CADDY_TAILNET_DOMAIN": "example.ts.net", "MEMORY_VAULT_PATH": "/srv/vault",
        "N8N_WEBHOOK_URL": "https://n8n.example.ts.net/", "HERMES_MAX_TOKENS": "32768",
        "HERMES_COMPRESSION_THRESHOLD_PERCENT": "0.8", "LLAMACPP_OVERRIDE_KV": "x=int:1",
        "LLAMACPP_CPU_MODEL": "cpu.gguf", "LLAMACPP_EMBED_MODEL": "embed.gguf"}
HW = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": "GPU-aaaa"},
               {"name": "GTX 1070", "vram_gb": 8, "uuid": "GPU-bbbb"}], "ram_gb": 128, "cpu_cores": 32}
# The operator's plugin set, so every service renders.
PLUGINS = ["comfyui", "song-gen", "voice", "rag", "qdrant-rag", "llamacpp-cpu", "open-webui", "automation",
           "searxng-web", "searxng", "codebase-memory-ui", "codebase-memory", "comfyui-mcp", "n8n",
           "orchestration", "hermes-dashboard", "monitoring", "memory-vault", "edge", "tailnet-names",
           "obsidian-livesync", "langfuse", "evals"]

SPEC: dict[str, set[str]] = {
    # scripts/llamacpp/run-llama-server.sh builds the llama-server argv from these.
    "llamacpp": {"LLAMACPP_MODEL", "LLAMACPP_CTX_SIZE", "LLAMACPP_PARALLEL", "LLAMACPP_ROPE_SCALING",
                 "LLAMACPP_ROPE_SCALE", "LLAMACPP_YARN_ORIG_CTX", "LLAMACPP_GPU_LAYERS", "LLAMACPP_FLASH_ATTN",
                 "LLAMACPP_N_PREDICT", "LLAMACPP_REASONING_BUDGET", "LLAMACPP_MMPROJ",
                 "LLAMACPP_ENABLE_KV_CACHE_QUANTIZATION", "LLAMACPP_KV_CACHE_TYPE_K", "LLAMACPP_KV_CACHE_TYPE_V",
                 "LLAMACPP_EXTRA_ARGS", "LLAMACPP_OVERRIDE_KV"},
    # services/model-gateway/entrypoint.sh writes these into LiteLLM's model_info.
    "model-gateway": {"LLAMACPP_CTX_SIZE", "LLAMACPP_N_PREDICT", "LLAMACPP_CPU_CTX", "LLAMACPP_MODEL",
                      "LLAMACPP_IMAGE", "LLAMACPP_MMPROJ", "LOCAL_INPUT_COST_PER_TOKEN",
                      "LOCAL_OUTPUT_COST_PER_TOKEN", "LLAMACPP_CPU_MODEL", "LLAMACPP_EMBED_MODEL"},
    # services_catalog.py builds the cards' Open links from the edge hostname / tailnet domain.
    "dashboard": {"CADDY_TAILNET_HOSTNAME", "CADDY_TAILNET_DOMAIN", "TAILNET_NAMES_ENABLED"},
    # The gated ComfyUI URL: the dialogue publisher, the reel/image scripts, the idle-reclaim cron and
    # the baked comfyui skill all submit to $COMFYUI_URL (AR2). Without it they fall back to a
    # direct address, which bypasses the GPU lease.
    "agent": {"COMFYUI_URL"},
    # + the two Hermes budget knobs: its entrypoint seeds them into the config.yaml it shares with the
    # agent, so without them a restart would reset the agent's tuned values to the defaults.
    "hermes-dashboard": {"COMFYUI_URL", "HERMES_MAX_TOKENS", "HERMES_COMPRESSION_THRESHOLD_PERCENT"},
    # The official couchdb image's docker-entrypoint.sh creates the admin from COUCHDB_USER.
    "couchdb": {"COUCHDB_USER"},
    # services/obsidian-livesync/entrypoint.sh (`:?` guards on all three).
    "livesync-bridge": {"COUCHDB_USER", "COUCHDB_INTERNAL_URL", "LIVESYNC_DATABASE"},
    # n8n's own config reads N8N_WEBHOOK_URL (@n8n/config `webhookUrl`), not only WEBHOOK_URL (AR3).
    "n8n": {"N8N_WEBHOOK_URL"},
}


@pytest.fixture(scope="module")
def rendered():
    rc = render(Source.from_dict({"hardware": HW, "model": "auto", "plugins": PLUGINS, "site": SITE}),
                CATALOG, REGISTRY)
    return rc, rc.compose_dict()["services"]


def _derived_refs(service: dict, env_keys: set[str]) -> set[str]:
    """The derived keys a service receives as a pass-through `KEY: ${KEY?...}` reference."""
    found = set()
    for name, value in (service.get("environment") or {}).items():
        if name in env_keys and re.fullmatch(rf"\$\{{{name}\?[^}}]*\}}", str(value)):
            found.add(name)
    return found


def test_no_service_loads_the_whole_env_file(rendered):
    _, services = rendered
    loaders = sorted(name for name, svc in services.items() if svc.get("env_file"))
    assert loaders == []


def test_each_service_receives_exactly_its_derived_keys(rendered):
    rc, services = rendered
    got = {name: _derived_refs(svc, set(rc.env)) for name, svc in services.items()}
    assert {k: v for k, v in got.items() if v} == SPEC


def test_every_env_name_is_declared(rendered):
    """A service's environment is its own env block + its declared derived keys + its secrets, and
    nothing else. Any rendered .env key that appears in a service's environment is either one it
    declared in derived_env or one its env block sets explicitly (e.g. the dashboard's
    `LLAMACPP_CTX_SIZE: ${LLAMACPP_CTX_SIZE:-...}`), never one that arrived by accident."""
    rc, services = rendered
    agent = AgentRegistry.load(ROOT / "services").default_agent()
    dashboard = DashboardRegistry.load(ROOT / "services").default_dashboard()
    declared: dict[str, set[str]] = {
        "llamacpp": set(compose_mod.LLAMACPP_DERIVED_ENV),
        "model-gateway": set(compose_mod.MODEL_GATEWAY_DERIVED_ENV),
        "agent": set(agent.environment) | set(agent.derived_env) | set(agent.secrets),
        "dashboard": set(dashboard.environment) | set(dashboard.derived_env) | set(dashboard.secrets),
    }
    for plugin in REGISTRY.plugins:
        for ps in plugin.services:
            declared[ps.name] = set(ps.env) | set(ps.derived_env) | set(ps.secrets)
    secret_names = set(rc.required_secrets) | set(OPTIONAL_SECRET_KEYS)
    for name, svc in services.items():
        env_names = set(svc.get("environment") or {})
        derived_here = env_names & set(rc.env)
        if name in declared:
            assert derived_here <= declared[name], (
                f"{name} receives {sorted(derived_here - declared[name])} without declaring them")
        else:  # MCP servers, gates, one-shots: only their own env, never a derived key by accident
            refs = {k for k, v in (svc.get("environment") or {}).items()
                    if k in rc.env and re.fullmatch(rf"\$\{{{k}\?[^}}]*\}}", str(v))}
            assert not refs, f"{name} receives derived keys {sorted(refs)} it never declared"
        if name in declared and name not in ("llamacpp", "model-gateway"):
            undeclared_secrets = {k for k in env_names & secret_names if k not in declared[name]}
            assert not undeclared_secrets, f"{name}: {sorted(undeclared_secrets)}"


def test_declared_derived_keys_are_real(rendered):
    """A typo in `derived_env:` would silently deliver nothing, so every declared name must be a key
    the render produces (with every plugin enabled and every runtime `site:` knob set)."""
    rc, _ = rendered
    agent = AgentRegistry.load(ROOT / "services").default_agent()
    dashboard = DashboardRegistry.load(ROOT / "services").default_dashboard()
    declared = {("core", "llamacpp"): set(compose_mod.LLAMACPP_DERIVED_ENV),
                ("core", "model-gateway"): set(compose_mod.MODEL_GATEWAY_DERIVED_ENV),
                ("agent", agent.id): set(agent.derived_env),
                ("dashboard", dashboard.id): set(dashboard.derived_env)}
    for plugin in REGISTRY.plugins:
        for ps in plugin.services:
            declared[(plugin.id, ps.name)] = set(ps.derived_env)
    known = set(rc.env)
    for where, names in declared.items():
        assert names <= known, f"{where} declares unknown derived keys {sorted(names - known)}"


def test_the_agent_and_every_comfyui_client_get_the_gated_url(rendered):
    """AR2, GPU safety. The agent's COMFYUI_URL used to arrive only through env_file. Every ComfyUI
    client must receive it explicitly, and it must resolve to the admission gate."""
    rc, services = rendered
    gate_url = rc.env[GATED_SERVICE_URL_ENV["comfyui"]]
    assert gate_url == "http://comfyui-gate:8188"
    for client in ("agent", "hermes-dashboard", "dashboard", "mcp-comfyui"):
        raw = services[client]["environment"]["COMFYUI_URL"]
        resolved = re.sub(r"\$\{(\w+)[^}]*\}", lambda m: rc.env[m.group(1)], raw)
        assert resolved == gate_url, f"{client} resolves COMFYUI_URL to {resolved!r}"


def test_a_key_the_render_did_not_produce_is_left_out():
    """Without comfyui there is no COMFYUI_URL in .env, so the agent gets no reference to it (a
    `${COMFYUI_URL?}` would fail every compose call on a stack without comfyui)."""
    plugins = [p for p in PLUGINS if p not in ("comfyui", "comfyui-mcp", "song-gen")]
    rc = render(Source.from_dict({"hardware": HW, "model": "auto", "plugins": plugins, "site": SITE}),
                CATALOG, REGISTRY)
    assert "COMFYUI_URL" not in rc.env
    agent_env = rc.compose_dict()["services"]["agent"].get("environment") or {}
    assert "COMFYUI_URL" not in agent_env


def test_ops_controller_holds_no_derived_key(rendered):
    """ops-controller runs `docker compose` itself, and compose prefers a process env value over
    --env-file. A derived key in its env would shadow the .env its own re-render just wrote."""
    rc, services = rendered
    assert not set(services["ops-controller"].get("environment") or {}) & set(rc.env)


def test_derived_refs_fail_loud_but_allow_empty(rendered):
    _, services = rendered
    assert services["llamacpp"]["environment"]["LLAMACPP_MMPROJ"] == \
        "${LLAMACPP_MMPROJ?LLAMACPP_MMPROJ is missing from the rendered .env}"


@pytest.mark.parametrize("make, env_block", [
    (lambda d: PluginService.from_dict({"name": "x", "image": "x:1", **d}), "env"),
    (lambda d: Agent.from_dict({"id": "x", **d}), "environment"),
    (lambda d: Dashboard.from_dict({"id": "x", **d}), "environment"),
])
def test_manifests_refuse_env_file_and_double_declaration(make, env_block):
    with pytest.raises(ValueError, match="derived_env"):
        make({"env_file": [".env"]})
    with pytest.raises(ValueError, match="derived_env"):
        make({env_block: {"COMFYUI_URL": "x"}, "derived_env": ["COMFYUI_URL"]})
