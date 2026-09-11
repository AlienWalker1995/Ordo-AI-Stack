"""kind=mcp plugins: rendered into a pinned mcp-gateway registry, drift-free."""
import json
from pathlib import Path

import pytest
import yaml

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import McpSpec, PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128}
P_CPU = {"gpus": [], "ram_gb": 16}
# The streamable-HTTP healthcheck every image-backed MCP service declares (a TCP connect to its own
# MCP port). Reused by the McpSpec validation tests below.
_HC = {"test": ["CMD", "python3", "-c", "import socket;socket.create_connection(('127.0.0.1',9000),5).close()"],
       "interval": "30s", "timeout": "5s", "retries": 3}


def _src(**kw):
    base = {"hardware": "auto", "tier": "auto", "model": "auto", "plugins": "auto"}
    base.update(kw)
    return Source.from_dict(base)


def test_mcp_plugins_loaded():
    mcp = {p.id for p in REGISTRY.plugins if p.kind == "mcp"}
    assert {"qdrant-rag", "searxng"} <= mcp


def test_render_emits_mcp_registry():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    ids = {s["id"] for s in rc.mcp_servers}
    assert {"qdrant-rag", "searxng"} <= ids
    q = next(s for s in rc.mcp_servers if s["id"] == "qdrant-rag")
    assert "qdrant_search" in q["tools"] and q["env"]["QDRANT_URL"]
    # mcp servers are NOT compose services — plugins_enabled holds only kind=service plugins
    assert "qdrant-rag" not in rc.plugins_enabled and "searxng" not in rc.plugins_enabled


def test_mcp_tools_available_even_on_cpu():
    rc = render(_src(hardware=P_CPU), CATALOG, REGISTRY)
    # no GPU → no GPU media plugins (monitoring is CPU-ok and stays)
    assert not ({"comfyui", "song-gen", "voice"} & set(rc.plugins_enabled))
    assert {s["id"] for s in rc.mcp_servers} >= {"qdrant-rag", "searxng"}  # but tools still work


def test_real_mcp_images_do_not_warn():
    # qdrant-rag = project buildable image (pinned by build context); searxng = real registry digest.
    # Neither should trip the unpinned/placeholder warnings anymore.
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    assert not any("placeholder" in w for w in rc.warnings)
    assert not any("not digest-pinned" in w for w in rc.warnings)


def test_placeholder_digest_still_detected():
    # the placeholder/unpinned detection itself must still fire for a bad public digest
    from ordo.plugins import Plugin
    from ordo.render import _render_mcp
    bad = Plugin.from_dict({"id": "bad", "kind": "mcp",
                            "mcp": {"image": "mcp/x@sha256:" + "0" * 64, "transport": "http",
                                    "port": 9000, "healthcheck": _HC}})
    _servers, notes = _render_mcp([bad])
    assert any("placeholder" in n for n in notes)


def test_write_emits_mcp_registry_yaml(tmp_path):
    render(_src(hardware=P_5090), CATALOG, REGISTRY).write(tmp_path)
    reg = yaml.safe_load((tmp_path / "mcp-registry.yaml").read_text())
    assert {s["id"] for s in reg["servers"]} >= {"qdrant-rag", "searxng"}


# ── Defect class: the mcp-gateway wrapper reads servers.txt + registry-custom.yaml (its native
#    schema) from the mounted config dir. render must emit those, or the gateway boots empty. ──
def test_write_emits_wrapper_native_mcp_config(tmp_path):
    render(_src(hardware=P_5090), CATALOG, REGISTRY).write(tmp_path)
    servers = (tmp_path / "mcp" / "servers.txt").read_text().strip()
    ids = set(servers.split(","))
    assert {"qdrant-rag", "searxng"} <= ids
    # registry-custom.yaml uses the wrapper's `registry:` map schema keyed by server id, env as list
    reg = yaml.safe_load((tmp_path / "mcp" / "registry-custom.yaml").read_text())
    assert "registry" in reg and {"qdrant-rag", "searxng"} <= set(reg["registry"])
    q = reg["registry"]["qdrant-rag"]
    assert q["type"] == "server" and q["image"] and isinstance(q["env"], list)
    assert any(e["name"] == "QDRANT_URL" for e in q["env"])


# ── Restored roster (V1→V2 migration dropped these): codebase-memory, comfyui, n8n, orchestration
#    must reappear in the rendered servers.txt + registry-custom.yaml with correct wiring. ──
RESTORED = {"codebase-memory", "comfyui", "n8n", "orchestration"}


def test_restored_mcp_plugins_loaded():
    mcp = {p.id for p in REGISTRY.plugins if p.kind == "mcp"}
    # plugin ids (comfyui-mcp decoupled from its server_id) — the manifests are present
    assert {"codebase-memory", "comfyui-mcp", "n8n", "orchestration"} <= mcp


def test_restored_servers_in_rendered_registry():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    ids = {s["id"] for s in rc.mcp_servers}
    # server ids (comfyui-mcp plugin maps to server_id `comfyui`) + the pre-existing three
    assert RESTORED <= ids
    assert {"qdrant-rag", "searxng", "memory-vault"} <= ids


def test_comfyui_server_id_decoupled_from_plugin_id():
    # the comfyui SERVICE plugin owns id `comfyui`; the MCP plugin is `comfyui-mcp` but its SERVER
    # id (compose service mcp-comfyui, Hermes tool prefix comfyui-) must be `comfyui`.
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    ids = {s["id"] for s in rc.mcp_servers}
    assert "comfyui" in ids and "comfyui-mcp" not in ids
    cm = next(s for s in rc.mcp_servers if s["id"] == "comfyui")
    assert cm["plugin_id"] == "comfyui-mcp" and cm["service"] == "mcp-comfyui"
    # ComfyUI's URL crosses this seam as a compose ${VAR} ref, so render can point COMFYUI_URL at
    # the admission gate when comfyui is gate-enforced. A literal here would pin the agent to the
    # DIRECT service and let every agent-submitted prompt bypass GPU arbitration.
    assert cm["env"]["COMFYUI_URL"] == "${COMFYUI_URL:-http://comfyui:8188}"
    assert cm["env"]["OPS_CONTROLLER_TOKEN"] == "${OPS_CONTROLLER_TOKEN}"
    assert cm["env"]["COMFY_MCP_DEFAULT_MODEL"] == "${COMFY_MCP_DEFAULT_MODEL:-flux1-schnell-fp8.safetensors}"
    # renders can take many minutes: the per-server LiteLLM tool timeout must cover a queue+wait
    assert cm["timeout"] == 1800


def test_codebase_memory_wiring():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    cb = next(s for s in rc.mcp_servers if s["id"] == "codebase-memory")
    assert cb["image"] == "ordo/codebase-memory-mcp:latest"
    # a 100% local indexer: internal MCP network only, so only LiteLLM can reach it
    assert cb["network"] == "internal"
    assert cb["url"] == "http://mcp-codebase-memory:9000/mcp" and cb["healthcheck"]
    # read-only host code-root bind (a compose ${VAR} ref) + named cache volume
    assert "${CODE_ROOT:-/c/dev}:/c/dev:ro" in cb["volumes"]
    assert "codebase-memory-cache:/cache" in cb["volumes"]
    assert cb["env"]["CBM_CACHE_DIR"] == "/cache"
    assert "index_repository" in cb["tools"] and "search_graph" in cb["tools"]


def test_n8n_digest_pinned_and_banner_suppressed():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    n8 = next(s for s in rc.mcp_servers if s["id"] == "n8n")
    # upstream czlonkowski/n8n-mcp, pinned by tag AND digest (never a floating catalog `latest`)
    assert n8["image"].startswith("ghcr.io/czlonkowski/n8n-mcp:2.84.1@sha256:")
    # HTTP transport: it listens on 3000 and LiteLLM presents the bearer named in secrets.env
    assert n8["port"] == 3000 and n8["env"]["MCP_MODE"] == "http"
    assert n8["auth_type"] == "bearer_token" and n8["auth_secret"] == "N8N_MCP_AUTH_TOKEN"
    # the banner/log suppression that keeps the FULL tool set (not docs-only ~23)
    assert n8["env"]["LOG_LEVEL"] == "error"
    assert n8["env"]["N8N_DIAGNOSTICS_ENABLED"] == "false"
    assert n8["env"]["DISABLE_TELEMETRY"] == "true"
    assert n8["env"]["N8N_API_KEY"] == "${N8N_API_KEY}"


def test_n8n_api_key_is_a_required_secret():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    assert "N8N_API_KEY" in rc.required_secrets  # emitted into secrets.env.example


def test_orchestration_wiring():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    orc = next(s for s in rc.mcp_servers if s["id"] == "orchestration")
    assert orc["env"]["ORCHESTRATION_DASHBOARD_URL"] == "http://dashboard:8080"
    # No per-service dashboard auth (edge SSO is the only gate) — orchestration carries no Bearer.
    assert "DASHBOARD_AUTH_TOKEN" not in orc["env"]
    # it must reach dashboard:8080, so it joins the stack network as well as the internal MCP one
    assert orc["network"] == "stack"


def test_restored_images_do_not_warn():
    # codebase-memory/comfyui/orchestration = project buildable images; n8n = real registry digest.
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    assert not any("placeholder" in w for w in rc.warnings)
    assert not any("not digest-pinned" in w for w in rc.warnings)


def test_server_id_collision_is_flagged():
    from ordo.plugins import Plugin
    from ordo.render import _render_mcp
    a = Plugin.from_dict({"id": "a", "kind": "mcp",
                          "mcp": {"image": "ordo/x:latest", "server_id": "shared",
                                  "transport": "http", "port": 9000, "healthcheck": _HC}})
    b = Plugin.from_dict({"id": "b", "kind": "mcp",
                          "mcp": {"image": "ordo/y:latest", "server_id": "shared",
                                  "transport": "http", "port": 9000, "healthcheck": _HC}})
    _servers, notes = _render_mcp([a, b])
    assert any("collides" in n for n in notes)


def test_restored_servers_in_written_servers_txt(tmp_path):
    render(_src(hardware=P_5090), CATALOG, REGISTRY).write(tmp_path)
    ids = set((tmp_path / "mcp" / "servers.txt").read_text().strip().split(","))
    assert RESTORED <= ids
    reg = yaml.safe_load((tmp_path / "mcp" / "registry-custom.yaml").read_text())
    assert RESTORED <= set(reg["registry"])
    # codebase-memory's declared volumes survive the write
    cb = reg["registry"]["codebase-memory"]
    assert "${CODE_ROOT:-/c/dev}:/c/dev:ro" in cb["volumes"]


# ── server_id → plugin_id map: emitted so the dashboard can persist a UI MCP toggle into ordo.yaml's
#    plugins list (servers.txt is render-owned and would otherwise reseed the toggle away). ──
def test_render_builds_server_plugin_map():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    m = rc.mcp_server_plugin_map
    # server_id → plugin_id; comfyui SERVER maps to the comfyui-mcp PLUGIN (decoupled id)
    assert m["comfyui"] == "comfyui-mcp"
    # the identity-mapped ones (server_id defaults to plugin id)
    assert m["qdrant-rag"] == "qdrant-rag" and m["searxng"] == "searxng"
    assert m["n8n"] == "n8n" and m["orchestration"] == "orchestration"
    assert m["codebase-memory"] == "codebase-memory" and m["memory-vault"] == "memory-vault"
    # keyed by SERVER id, never the decoupled plugin id
    assert "comfyui-mcp" not in m


def test_map_covers_all_registered_mcp_plugins_even_when_disabled():
    # CPU render disables GPU media plugins, but the map must still cover EVERY registered kind=mcp
    # plugin (enabled + available-but-disabled) so the UI can re-enable one. comfyui-mcp depends on
    # the comfyui service (GPU) so it isn't ENABLED on CPU — but it must still be in the map.
    rc_cpu = render(_src(hardware=P_CPU), CATALOG, REGISTRY)
    all_mcp_plugins = {p.id for p in REGISTRY.plugins if p.kind == "mcp"}
    assert set(rc_cpu.mcp_server_plugin_map.values()) == all_mcp_plugins
    assert rc_cpu.mcp_server_plugin_map["comfyui"] == "comfyui-mcp"  # disabled here, still mapped


def test_write_emits_server_plugin_map_json(tmp_path):
    render(_src(hardware=P_5090), CATALOG, REGISTRY).write(tmp_path)
    m = json.loads((tmp_path / "mcp" / "server-plugin-map.json").read_text())
    assert m["comfyui"] == "comfyui-mcp"
    assert {"qdrant-rag", "searxng", "n8n", "orchestration",
            "codebase-memory", "memory-vault"} <= set(m)
    # lives alongside servers.txt in the dir the dashboard mounts at /mcp-config
    assert (tmp_path / "mcp" / "servers.txt").exists()


# ── McpSpec: the validated `mcp:` manifest block. One streamable-HTTP server per plugin, either a
#    compose service built from `image` or a hosted `url`. Invalid shapes fail at manifest load. ──
def test_mcp_spec_image_server_requires_http_port_and_healthcheck():
    spec = McpSpec.from_dict({"image": "ordo/x-mcp:latest", "transport": "http", "port": 9000,
                              "healthcheck": _HC, "network": "stack"}, plugin_id="x")
    assert spec.server_id == "x" and spec.service_name == "mcp-x"
    assert spec.internal_url() == "http://mcp-x:9000/mcp" and not spec.hosted
    for bad in (
        {"image": "ordo/x:latest", "port": 9000, "healthcheck": _HC},                         # no transport
        {"image": "ordo/x:latest", "transport": "stdio", "port": 9000, "healthcheck": _HC},   # stdio
        {"image": "ordo/x:latest", "transport": "http", "healthcheck": _HC},                  # no port
        {"image": "ordo/x:latest", "transport": "http", "port": 9000},                        # no healthcheck
        {"image": "ordo/x:latest", "transport": "http", "port": 9000, "healthcheck": _HC, "network": "host"},
        {"image": "ordo/x:latest", "transport": "http", "port": 9000, "healthcheck": _HC, "longLived": True},
        {"image": "ordo/x:latest", "url": "https://h/mcp", "transport": "http", "port": 9000, "healthcheck": _HC},
        {"image": "ordo/x:latest", "transport": "http", "port": 9000, "healthcheck": _HC, "auth": {"type": "bearer_token"}},
    ):
        with pytest.raises(ValueError):
            McpSpec.from_dict(bad, plugin_id="x")


def test_mcp_spec_hosted_server_has_no_container():
    spec = McpSpec.from_dict({"url": "https://mcp.example.com/mcp", "transport": "http"}, plugin_id="ext")
    assert spec.hosted and spec.service_name == "" and spec.internal_url() == "https://mcp.example.com/mcp"


def test_mcp_spec_auth_and_timeout():
    spec = McpSpec.from_dict({"image": "i:1@sha256:" + "a" * 64, "transport": "http", "port": 3000,
                              "healthcheck": _HC, "timeout": 1800,
                              "auth": {"type": "bearer_token", "secret": "N8N_MCP_AUTH_TOKEN"}}, plugin_id="n")
    assert spec.timeout == 1800 and spec.auth_type == "bearer_token" and spec.auth_secret == "N8N_MCP_AUTH_TOKEN"


def test_all_registered_mcp_manifests_validate_and_declare_http():
    for p in REGISTRY.plugins:
        if p.kind == "mcp":
            assert p.mcp is not None and p.mcp.transport == "http", p.id
            assert p.mcp.hosted or (p.mcp.port > 0 and p.mcp.healthcheck), p.id
