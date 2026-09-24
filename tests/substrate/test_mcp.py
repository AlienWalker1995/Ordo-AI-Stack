"""kind=mcp plugins: rendered into pinned mcp-<id> compose services + the LiteLLM fragment, drift-free."""
import json
from pathlib import Path

import pytest
import yaml

from ordo import compose
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import McpSpec, PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128}
P_CPU = {"gpus": [], "ram_gb": 16}
# A manifest healthcheck OVERRIDE (the renderer supplies the default probe; searxng-mcp is the one
# real manifest that overrides it, because that image ships node and not python3). Reused by the
# McpSpec validation tests below.
_HC = {"test": ["CMD", "node", "-e", "require('net').connect(9000,'127.0.0.1')"],
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


# ── Defect class: LiteLLM reads its MCP servers from the rendered `mcp_servers` fragment, and the
#    dashboard reads the enabled roster from servers.json. render must emit both, or the gateway
#    exposes no tools and the UI shows nothing. ──
def test_write_emits_litellm_fragment_and_servers_json(tmp_path):
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    rc.write(tmp_path)
    frag = yaml.safe_load((tmp_path / "model-gateway" / "mcp_servers.yaml").read_text())
    servers = frag["mcp_servers"]
    # LiteLLM keys/names are hyphen-free (the hyphen is its tool-prefix separator); the URL keeps
    # the hyphenated compose service name.
    assert {"qdrant_rag", "searxng"} <= set(servers)
    q = servers["qdrant_rag"]
    assert q == {"server_id": "qdrant_rag", "url": "http://mcp-qdrant-rag:9000/mcp", "transport": "http",
                 "description": q["description"], "timeout": 60, "available_on_public_internet": False,
                 "mcp_info": {"server_name": "qdrant_rag"}}
    assert servers["searxng"]["url"] == "http://mcp-searxng:8080/mcp"
    # the dashboard's view: enabled servers + the full server_id -> plugin_id map
    sj = json.loads((tmp_path / "mcp" / "servers.json").read_text())
    ids = {s["id"] for s in sj["servers"]}
    assert {"qdrant-rag", "searxng"} <= ids          # servers.json ids stay the hyphenated server_id
    assert all(s.get("litellm_name") for s in sj["servers"])
    assert {s["id"]: s["litellm_name"] for s in sj["servers"]}["qdrant-rag"] == "qdrant_rag"
    assert sj["plugin_map"]["comfyui"] == "comfyui-mcp"
    # the Docker-gateway artefacts are gone
    for gone in ("mcp/servers.txt", "mcp/registry-custom.yaml", "mcp/server-plugin-map.json", "mcp-registry.yaml"):
        assert not (tmp_path / gone).exists(), gone


def test_litellm_names_never_contain_the_tool_separator(tmp_path):
    """LiteLLM 1.100.1 rejects a server name containing MCP_TOOL_PREFIX_SEPARATOR (`-`), and tools
    reach clients as `<server_name>-<tool>`. Every rendered name must therefore be hyphen-free."""
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    rc.write(tmp_path)
    frag = yaml.safe_load((tmp_path / "model-gateway" / "mcp_servers.yaml").read_text())["mcp_servers"]
    assert frag, "the render emitted no MCP servers"
    for key, entry in frag.items():
        assert "-" not in key, key
        assert "-" not in entry["server_id"], entry["server_id"]
        assert "-" not in entry["mcp_info"]["server_name"], entry["mcp_info"]["server_name"]


def test_litellm_name_collision_is_a_hard_error():
    """`a-b` and `a_b` are distinct server_ids that render to ONE LiteLLM name. The fragment is a
    name-keyed map, so the second would overwrite the first and that server would vanish from the
    gateway: refuse the render (as the component doc promises) instead of noting it."""
    from ordo.plugins import Plugin
    from ordo.render import _render_mcp
    digest = "0123456789abcdef" * 4          # varied, so the placeholder-digest note stays silent
    hyphen = Plugin.from_dict({"id": "a-b", "kind": "mcp",
                               "mcp": {"image": f"i:1@sha256:{digest}", "transport": "http",
                                       "port": 9000, "healthcheck": _HC}})
    under = Plugin.from_dict({"id": "a_b", "kind": "mcp",
                              "mcp": {"image": f"i:2@sha256:{digest}", "transport": "http",
                                      "port": 9000, "healthcheck": _HC}})
    with pytest.raises(ValueError, match="a_b"):
        _render_mcp([hyphen, under])


def test_fragment_carries_auth_timeout_and_explicit_defaults():
    from ordo.plugins import Plugin
    from ordo.render import _render_mcp, render_litellm_mcp_fragment
    # No registered server currently declares upstream auth (the bridged servers listen on the
    # internal MCP network only), so drive the auth path from a synthetic manifest.
    authed = Plugin.from_dict({"id": "authed", "kind": "mcp",
                               "mcp": {"image": "i:1@sha256:" + "a" * 64, "transport": "http",
                                       "port": 9000, "healthcheck": _HC,
                                       "auth": {"type": "bearer_token", "secret": "SOME_TOKEN"}}})
    servers, _ = _render_mcp([p for p in REGISTRY.plugins if p.id == "comfyui-mcp"] + [authed])
    frag = yaml.safe_load(render_litellm_mcp_fragment(servers))["mcp_servers"]
    assert frag["authed"]["auth_type"] == "bearer_token"
    assert frag["authed"]["auth_value"] == "os.environ/SOME_TOKEN"
    assert frag["comfyui"]["timeout"] == 1800
    for s in frag.values():          # both documented defaults disagree with LiteLLM's code: always explicit
        assert s["transport"] == "http" and s["available_on_public_internet"] is False


def test_hosted_server_renders_no_compose_service():
    from ordo.plugins import Plugin
    from ordo.render import _render_mcp, render_litellm_mcp_fragment
    hosted = Plugin.from_dict({"id": "ext", "kind": "mcp", "mcp": {"url": "https://h.example/mcp", "transport": "http"}})
    servers, notes = _render_mcp([hosted])
    assert notes == [] and servers[0]["hosted"] and servers[0]["service"] == ""
    c = compose.render_compose(has_gpu=False, compose_profiles=[], mcp_servers=servers)
    assert not any(n.startswith("mcp-") for n in c["services"])
    assert yaml.safe_load(render_litellm_mcp_fragment(servers))["mcp_servers"]["ext"]["url"] == "https://h.example/mcp"


# ── Restored roster (V1→V2 migration dropped these): codebase-memory, comfyui, n8n, orchestration
#    must reappear in the rendered servers.json + compose services with correct wiring. ──
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
    assert cb["url"] == "http://mcp-codebase-memory:9000/mcp"
    # no manifest healthcheck: the renderer supplies the default probe (test_compose.py)
    assert cb["healthcheck"] == {}
    # read-only host code-root bind (a compose ${VAR} ref) + named cache volume
    assert "${CODE_ROOT:-/c/dev}:/c/dev:ro" in cb["volumes"]
    assert "codebase-memory-cache:/cache" in cb["volumes"]
    assert cb["env"]["CBM_CACHE_DIR"] == "/cache"
    assert "index_repository" in cb["tools"] and "search_graph" in cb["tools"]


def test_n8n_bridged_and_banner_suppressed():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    n8 = next(s for s in rc.mcp_servers if s["id"] == "n8n")
    # Project-built stdio bridge (services/n8n/Dockerfile) over the pinned upstream image: the
    # upstream's own HTTP mode is session-ful and LiteLLM cannot hold a session across operations.
    assert n8["image"] == "ordo/n8n-mcp:latest"
    assert n8["port"] == 9000
    # No upstream bearer: the bridge listens on the internal MCP network only.
    assert n8["auth_type"] == "" and n8["auth_secret"] == ""
    # the banner/log suppression: under the stdio bridge a stray stdout byte corrupts JSON-RPC,
    # and these also keep the FULL tool set (not the docs-only subset)
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



def test_restored_servers_in_written_servers_json(tmp_path):
    render(_src(hardware=P_5090), CATALOG, REGISTRY).write(tmp_path)
    ids = {s["id"] for s in json.loads((tmp_path / "mcp" / "servers.json").read_text())["servers"]}
    assert RESTORED <= ids
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert {f"mcp-{i}" for i in RESTORED} <= set(c["services"])
    # codebase-memory's declared volumes survive the write, onto its own compose service
    cb = c["services"]["mcp-codebase-memory"]
    assert "${CODE_ROOT:-/c/dev}:/c/dev:ro" in cb["volumes"]


# ── server_id → plugin_id map: emitted so the dashboard can persist a UI MCP toggle into ordo.yaml's
#    plugins list (the enabled roster is render-owned and would otherwise reseed the toggle away). ──
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


def test_write_emits_server_plugin_map_in_servers_json(tmp_path):
    render(_src(hardware=P_5090), CATALOG, REGISTRY).write(tmp_path)
    m = json.loads((tmp_path / "mcp" / "servers.json").read_text())["plugin_map"]
    assert m["comfyui"] == "comfyui-mcp"
    assert {"qdrant-rag", "searxng", "n8n", "orchestration",
            "codebase-memory", "memory-vault"} <= set(m)
    # servers.json is the ONLY file in the dir the dashboard mounts at /mcp-config
    assert [p.name for p in (tmp_path / "mcp").iterdir()] == ["servers.json"]



def test_write_prunes_the_retired_mcp_gateway_artefacts(tmp_path):
    """A re-render over an existing out/ removes the files older renders emitted: they sat in the
    dir the dashboard mounts, looking live. Operator-owned files in out/ are never touched."""
    retired = ("mcp/servers.txt", "mcp/registry-custom.yaml", "mcp/registry-custom.docker.yaml",
               "mcp/server-plugin-map.json", "mcp-registry.yaml")
    operator_owned = ("ordo.yaml", "secrets.env", "lease-history.jsonl", "auth/caddy/tailnet.crt")
    for rel in retired + operator_owned:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("stale\n", encoding="utf-8")
    render(_src(hardware=P_5090), CATALOG, REGISTRY).write(tmp_path)
    assert [rel for rel in retired if (tmp_path / rel).exists()] == []
    assert [rel for rel in operator_owned if not (tmp_path / rel).exists()] == []
    assert [p.name for p in (tmp_path / "mcp").iterdir()] == ["servers.json"]

# ── McpSpec: the validated `mcp:` manifest block. One streamable-HTTP server per plugin, either a
#    compose service built from `image` or a hosted `url`. Invalid shapes fail at manifest load. ──
def test_mcp_spec_image_server_requires_http_and_a_port():
    spec = McpSpec.from_dict({"image": "ordo/x-mcp:latest", "transport": "http", "port": 9000,
                              "healthcheck": _HC, "network": "stack"}, plugin_id="x")
    assert spec.server_id == "x" and spec.service_name == "mcp-x"
    assert spec.internal_url() == "http://mcp-x:9000/mcp" and not spec.hosted
    for bad in (
        {"image": "ordo/x:latest", "port": 9000, "healthcheck": _HC},                         # no transport
        {"image": "ordo/x:latest", "transport": "stdio", "port": 9000, "healthcheck": _HC},   # stdio
        {"image": "ordo/x:latest", "transport": "http", "healthcheck": _HC},                  # no port
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
                              "auth": {"type": "bearer_token", "secret": "SOME_TOKEN"}}, plugin_id="n")
    assert spec.timeout == 1800 and spec.auth_type == "bearer_token" and spec.auth_secret == "SOME_TOKEN"


def test_all_registered_mcp_manifests_validate_and_declare_http():
    for p in REGISTRY.plugins:
        if p.kind == "mcp":
            assert p.mcp is not None and p.mcp.transport == "http", p.id
            assert p.mcp.hosted or p.mcp.port > 0, p.id
