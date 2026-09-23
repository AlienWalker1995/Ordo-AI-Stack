"""Each service receives only the secrets it needs.

Secrets used to reach 21 services through a shared `env_file: secrets.env`, so every one of them,
tailnet sidecars included, held every credential. Now a service declares the secret NAMES it reads
(`secrets:` on a plugin service, agent or dashboard manifest, or the core service's own list in
ordo/compose.py) and the renderer passes exactly those as `KEY: ${KEY}`; compose interpolates the
values from `--env-file secrets.env`.

SPEC below is the security contract, derived from what each process actually reads (code, image
entrypoints, the agent's skills). Giving a service another secret means changing SPEC on purpose.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry, PluginService
from ordo.render import CORE_SECRET_KEYS, OPTIONAL_SECRET_KEYS, render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
HW = {"gpus": [{"name": "RTX 5090", "vram_gb": 32, "uuid": "GPU-aaaa"},
               {"name": "GTX 1070", "vram_gb": 8, "uuid": "GPU-bbbb"}], "ram_gb": 128, "cpu_cores": 32}
# The same plugin set the operator's stack runs, so every secret-bearing service renders.
PLUGINS = ["comfyui", "song-gen", "voice", "rag", "qdrant-rag", "llamacpp-cpu", "open-webui", "automation",
           "searxng-web", "searxng", "codebase-memory-ui", "codebase-memory", "comfyui-mcp", "n8n",
           "orchestration", "hermes-dashboard", "monitoring", "memory-vault", "edge", "tailnet-names",
           "obsidian-livesync", "langfuse", "evals"]

TAILNET = {"TS_AUTHKEY"}
LANGFUSE_PAIR = {"LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"}
SPEC: dict[str, set[str]] = {
    # (+ GOOGLE_CLIENT_ID/SECRET <- OAUTH2_PROXY_CLIENT_* when a site edge hostname enables LiteLLM
    # SSO; that wiring has its own tests in test_litellm_google_sso.py)
    "model-gateway": {"LITELLM_MASTER_KEY", "LITELLM_SALT_KEY", "LITELLM_DB_PASSWORD",
                      "THROUGHPUT_RECORD_TOKEN"} | LANGFUSE_PAIR,
    "model-gateway-keys": {"LITELLM_MASTER_KEY", "LITELLM_KEY_HERMES", "LITELLM_KEY_AUTOMATION", "LITELLM_KEY_EDGE",
                           "LITELLM_KEY_EVALS", "LITELLM_KEY_OPEN_WEBUI"},
    "litellm-db": {"LITELLM_DB_PASSWORD"},
    "ops-controller": {"OPS_CONTROLLER_TOKEN"},
    "dashboard": {"OPS_CONTROLLER_TOKEN", "THROUGHPUT_RECORD_TOKEN", "LITELLM_MASTER_KEY"},
    "agent": {"LITELLM_KEY_HERMES", "OPS_CONTROLLER_TOKEN", "HERMES_API_SERVER_KEY"} | LANGFUSE_PAIR,
    "hermes-dashboard": {"LITELLM_KEY_HERMES", "OPS_CONTROLLER_TOKEN"} | LANGFUSE_PAIR,
    "comfyui": {"OPS_CONTROLLER_TOKEN", "LITELLM_MASTER_KEY", "HF_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN"},
    "comfyui-gate": {"OPS_CONTROLLER_TOKEN"},
    "mcp-comfyui": {"OPS_CONTROLLER_TOKEN"},
    "mcp-n8n": {"N8N_API_KEY"},
    "n8n": {"LITELLM_KEY_AUTOMATION"},
    "open-webui": {"LITELLM_KEY_OPEN_WEBUI"},
    "evals": {"OPS_CONTROLLER_TOKEN", "LITELLM_KEY_EVALS", "HERMES_API_SERVER_KEY"} | LANGFUSE_PAIR,
    "couchdb": {"COUCHDB_PASSWORD"},
    "livesync-bridge": {"COUCHDB_PASSWORD", "LIVESYNC_E2EE_PASSPHRASE"},
    "oauth2-proxy": {"OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET", "OAUTH2_PROXY_COOKIE_SECRET"},
    "searxng": {"SEARXNG_SECRET"},
    "langfuse-db": {"LANGFUSE_DB_PASSWORD"},
    "langfuse-redis": {"LANGFUSE_REDIS_AUTH"},
    "langfuse-clickhouse": {"LANGFUSE_CLICKHOUSE_PASSWORD"},
    "langfuse-minio": {"LANGFUSE_MINIO_SECRET"},
    "langfuse-minio-lifecycle": {"LANGFUSE_MINIO_SECRET"},
    "langfuse-retention": LANGFUSE_PAIR,
    "langfuse-web": {"LANGFUSE_CLICKHOUSE_PASSWORD", "LANGFUSE_DB_PASSWORD", "LANGFUSE_ENCRYPTION_KEY",
                     "LANGFUSE_ADMIN_PASSWORD", "LANGFUSE_MINIO_SECRET", "LANGFUSE_NEXTAUTH_SECRET",
                     "LANGFUSE_REDIS_AUTH", "LANGFUSE_SALT"} | LANGFUSE_PAIR,
    "langfuse-worker": {"LANGFUSE_CLICKHOUSE_PASSWORD", "LANGFUSE_DB_PASSWORD", "LANGFUSE_ENCRYPTION_KEY",
                        "LANGFUSE_MINIO_SECRET", "LANGFUSE_REDIS_AUTH", "LANGFUSE_SALT"},
    **{f"tailnet-{n}": TAILNET for n in ("chat", "comfy", "dash", "graph", "hermes", "langfuse", "llm", "n8n")},
}


@pytest.fixture(scope="module")
def rendered():
    rc = render(Source.from_dict({"hardware": HW, "model": "auto", "plugins": PLUGINS}), CATALOG, REGISTRY)
    return rc, rc.compose_dict()


def _secret_names(rc) -> set[str]:
    return set(rc.required_secrets) | set(OPTIONAL_SECRET_KEYS)


def _delivered(service: dict, names: set[str]) -> set[str]:
    """Every secret NAME a service's environment references, as `KEY: ${KEY}` or renamed."""
    found = set()
    for value in (service.get("environment") or {}).values():
        for ref in re.findall(r"\$\{([A-Z0-9_]+)", str(value)):
            if ref in names:
                found.add(ref)
    return found


def test_no_service_loads_the_whole_secrets_file(rendered):
    _, compose = rendered
    for name, svc in compose["services"].items():
        files = [f if isinstance(f, str) else f.get("path") for f in svc.get("env_file", [])]
        assert "secrets.env" not in files, f"{name} still loads every secret"


def test_each_service_receives_exactly_its_secrets(rendered):
    rc, compose = rendered
    names = _secret_names(rc)
    got = {name: _delivered(svc, names) for name, svc in compose["services"].items()}
    got = {k: v for k, v in got.items() if v}
    assert got == SPEC


def test_the_ops_token_reaches_only_its_callers(rendered):
    rc, compose = rendered
    holders = {n for n, svc in compose["services"].items() if "OPS_CONTROLLER_TOKEN" in _delivered(svc, {"OPS_CONTROLLER_TOKEN"})}
    assert holders == {"ops-controller", "dashboard", "agent", "hermes-dashboard", "comfyui", "comfyui-gate",
                       "mcp-comfyui", "evals"}


def test_declared_secrets_are_provisioned(rendered):
    # Every secret a manifest declares must be listed in secrets.env.example (or be a known optional
    # one), or the operator is never asked for it and the service starts with an empty value.
    from ordo.agents import AgentRegistry
    from ordo.dashboards import DashboardRegistry

    rc, _ = rendered
    names = _secret_names(rc)
    services_dir = ROOT / "services"
    enabled = set(PLUGINS)
    declared = {f"{p.id}/{ps.name}": set(ps.secrets)
                for p in REGISTRY.plugins if p.id in enabled for ps in p.services}
    declared |= {f"agent/{a.id}": set(a.secrets) for a in [AgentRegistry.load(services_dir).default_agent()]}
    declared |= {f"dashboard/{d.id}": set(d.secrets) for d in [DashboardRegistry.load(services_dir).default_dashboard()]}
    for where, keys in declared.items():
        assert keys <= names, f"{where} declares {sorted(keys - names)}, which secrets.env.example does not list"


def test_core_secret_keys_stay_required(rendered):
    rc, _ = rendered
    assert set(CORE_SECRET_KEYS) <= set(rc.required_secrets)


def test_the_old_whole_file_flag_is_rejected():
    with pytest.raises(ValueError, match="secrets"):
        PluginService.from_dict({"name": "x", "image": "x:1", "wants_secrets": True})
