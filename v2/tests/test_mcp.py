"""kind=mcp plugins: rendered into a pinned mcp-gateway registry, drift-free."""
from pathlib import Path

import yaml

from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")
P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128}
P_CPU = {"gpus": [], "ram_gb": 16}


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
                            "mcp": {"image": "mcp/x@sha256:" + "0" * 64}})
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
    # the comfyui SERVICE plugin owns id `comfyui`; the MCP plugin is `comfyui-mcp` but its gateway
    # registry key (Hermes tool namespace comfyui__*) must be `comfyui`, not `comfyui-mcp`.
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    ids = {s["id"] for s in rc.mcp_servers}
    assert "comfyui" in ids and "comfyui-mcp" not in ids
    cm = next(s for s in rc.mcp_servers if s["id"] == "comfyui")
    assert cm["env"]["COMFYUI_URL"] == "http://comfyui:8188"
    assert cm["env"]["OPS_CONTROLLER_TOKEN"] == "PLACEHOLDER_OPS_CONTROLLER_TOKEN"
    assert cm["env"]["COMFY_MCP_DEFAULT_MODEL"] == "PLACEHOLDER_COMFY_MCP_DEFAULT_MODEL"


def test_codebase_memory_wiring():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    cb = next(s for s in rc.mcp_servers if s["id"] == "codebase-memory")
    assert cb["image"] == "ordo-v2/codebase-memory-mcp:latest"
    assert cb["longLived"] is True and cb["disableNetwork"] is True
    # read-only host code-root bind (placeholder wrapper-substituted) + named cache volume
    assert "PLACEHOLDER_CODE_ROOT:/c/dev:ro" in cb["volumes"]
    assert "codebase-memory-cache:/cache" in cb["volumes"]
    assert cb["env"]["CBM_CACHE_DIR"] == "/cache"
    assert "index_repository" in cb["tools"] and "search_graph" in cb["tools"]


def test_n8n_digest_pinned_and_banner_suppressed():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    n8 = next(s for s in rc.mcp_servers if s["id"] == "n8n")
    assert n8["image"].startswith("mcp/n8n@sha256:")  # pinned, not the online-catalog roulette
    # the stdout-banner suppression that keeps the FULL tool set (not docs-only ~23)
    assert n8["env"]["LOG_LEVEL"] == "error"
    assert n8["env"]["N8N_DIAGNOSTICS_ENABLED"] == "false"
    assert n8["env"]["DISABLE_TELEMETRY"] == "true"
    assert n8["env"]["N8N_API_KEY"] == "PLACEHOLDER_N8N_API_KEY"


def test_n8n_api_key_is_a_required_secret():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    assert "N8N_API_KEY" in rc.required_secrets  # emitted into secrets.env.example


def test_orchestration_wiring():
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    orc = next(s for s in rc.mcp_servers if s["id"] == "orchestration")
    assert orc["env"]["ORCHESTRATION_DASHBOARD_URL"] == "http://dashboard:8080"
    assert orc["env"]["DASHBOARD_AUTH_TOKEN"] == "PLACEHOLDER_DASHBOARD_AUTH_TOKEN"
    assert orc["disableNetwork"] is False


def test_restored_images_do_not_warn():
    # codebase-memory/comfyui/orchestration = project buildable images; n8n = real registry digest.
    rc = render(_src(hardware=P_5090), CATALOG, REGISTRY)
    assert not any("placeholder" in w for w in rc.warnings)
    assert not any("not digest-pinned" in w for w in rc.warnings)


def test_server_id_collision_is_flagged():
    from ordo.plugins import Plugin
    from ordo.render import _render_mcp
    a = Plugin.from_dict({"id": "a", "kind": "mcp",
                          "mcp": {"image": "ordo-v2/x:latest", "server_id": "shared"}})
    b = Plugin.from_dict({"id": "b", "kind": "mcp",
                          "mcp": {"image": "ordo-v2/y:latest", "server_id": "shared"}})
    _servers, notes = _render_mcp([a, b])
    assert any("collides" in n for n in notes)


def test_restored_servers_in_written_servers_txt(tmp_path):
    render(_src(hardware=P_5090), CATALOG, REGISTRY).write(tmp_path)
    ids = set((tmp_path / "mcp" / "servers.txt").read_text().strip().split(","))
    assert RESTORED <= ids
    reg = yaml.safe_load((tmp_path / "mcp" / "registry-custom.yaml").read_text())
    assert RESTORED <= set(reg["registry"])
    # codebase-memory catalog passthrough (volumes/longLived/disableNetwork) survives the write
    cb = reg["registry"]["codebase-memory"]
    assert cb["longLived"] is True and cb["disableNetwork"] is True
    assert "PLACEHOLDER_CODE_ROOT:/c/dev:ro" in cb["volumes"]
