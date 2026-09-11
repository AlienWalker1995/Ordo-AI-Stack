"""Memory-vault feature: the file-based memory-vault MCP plugin (plus the generic
service-renderer shm_size passthrough it shares the render path with).

Covers the manifests for a shared markdown memory vault:
  - memory-vault (kind=mcp): renders into an MCP server record with a READ-WRITE vault volume,
    the internal-only network, and its tool set, proving the render engine passes the
    file-based-MCP fields through (they were previously dropped).
  - shm_size: the generic service-renderer field (reusable), passed through when declared and
    omitted otherwise.
"""
from pathlib import Path

import yaml

from ordo.catalog import Catalog
from ordo.compose import _plugin_service
from ordo.config import Source
from ordo.plugins import Plugin, PluginRegistry, PluginService
from ordo.render import _render_mcp, render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
P_5090 = {"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128}
P_CPU = {"gpus": [], "ram_gb": 16}
# The vault bind the memory-vault manifest declares: a fail-loud compose ref, so a render without
# site.MEMORY_VAULT_PATH refuses at `docker compose config` instead of mounting an empty dir.
_VAULT_VOLUME = "${MEMORY_VAULT_PATH:?MEMORY_VAULT_PATH must be set in ordo.yaml site}:/vault"
_HC = {"test": ["CMD", "python3", "-c",
                "import socket;socket.create_connection(('127.0.0.1',9000),5).close()"],
       "interval": "30s", "timeout": "5s", "retries": 3}


def _src(plugins, hardware=P_5090):
    return Source.from_dict(
        {"hardware": hardware, "tier": "auto", "model": "auto", "plugins": plugins}
    )


# ── generic service-renderer shm_size passthrough ────────────────────────────
def test_plugin_service_shm_size_passthrough_and_omit():
    # data-driven: a service that declares shm_size emits it; one that doesn't omits the key
    # entirely (so no service regresses to an explicit-but-empty shm_size).
    p = Plugin.from_dict({"id": "x", "kind": "service", "compose_profile": "x", "services": []})
    kw = dict(net="ordo-net", env_file=".env", has_gpu=False,
              primary_uuid=None, secondary_uuid=None, project="ordo")
    with_shm = _plugin_service(
        PluginService.from_dict({"name": "s1", "image": "img", "shm_size": "1gb"}), p, **kw)
    assert with_shm["shm_size"] == "1gb"
    without = _plugin_service(PluginService.from_dict({"name": "s2", "image": "img"}), p, **kw)
    assert "shm_size" not in without


# ── memory-vault file-based MCP plugin ───────────────────────────────────────
def test_memory_vault_manifest_loaded():
    mcp = {p.id for p in REGISTRY.plugins if p.kind == "mcp"}
    assert "memory-vault" in mcp


def test_memory_vault_mcp_render_passes_through_catalog_fields():
    rc = render(_src(["memory-vault"]), CATALOG, REGISTRY)
    mv = next(s for s in rc.mcp_servers if s["id"] == "memory-vault")
    assert mv["image"] == "ordo/mcpvault-mcp:latest"
    # a pure-fs tool: internal MCP network only, so only LiteLLM can reach it
    assert mv["network"] == "internal"
    assert mv["url"] == "http://mcp-memory-vault:9000/mcp" and mv["healthcheck"]
    # the vault volume is a HOST bind (a fail-loud compose ${VAR:?} ref) and READ-WRITE (no :ro)
    assert mv["volumes"] == [_VAULT_VOLUME]
    assert not any(v.endswith(":ro") for v in mv["volumes"]), "vault must be writable by the MCP"
    # its tool surface
    assert {"read_note", "write_note", "patch_note", "search_notes"} <= set(mv["tools"])
    # project buildable image → no unpinned/placeholder warning
    assert not any("memory-vault" in w and "pinned" in w for w in rc.warnings)


def test_memory_vault_registry_custom_yaml_has_rw_vault(tmp_path):
    render(_src(["memory-vault"]), CATALOG, REGISTRY).write(tmp_path)
    # servers.txt lists it
    ids = set((tmp_path / "mcp" / "servers.txt").read_text().strip().split(","))
    assert "memory-vault" in ids
    # registry-custom.yaml carries the vault volume + hygiene flags through to the gateway catalog
    reg = yaml.safe_load((tmp_path / "mcp" / "registry-custom.yaml").read_text())
    mv = reg["registry"]["memory-vault"]
    assert mv["type"] == "server"
    assert mv["image"] == "ordo/mcpvault-mcp:latest"
    assert mv["volumes"] == [_VAULT_VOLUME]


def test_existing_mcp_entries_unchanged_by_passthrough(tmp_path):
    # image+env-only MCP plugins must NOT sprout empty volumes/command/longLived/disableNetwork
    # keys: the passthrough is opt-in so their rendered catalog entry stays byte-stable.
    render(_src("auto"), CATALOG, REGISTRY).write(tmp_path)
    reg = yaml.safe_load((tmp_path / "mcp" / "registry-custom.yaml").read_text())
    for pid in ("qdrant-rag", "searxng"):
        entry = reg["registry"][pid]
        assert "volumes" not in entry
        assert "command" not in entry
        assert "longLived" not in entry
        assert "disableNetwork" not in entry


def test_memory_vault_tools_available_even_on_cpu():
    # pure-fs tool: no GPU dependency, so it renders on a CPU-only box too
    rc = render(_src(["memory-vault"], hardware=P_CPU), CATALOG, REGISTRY)
    assert "memory-vault" in {s["id"] for s in rc.mcp_servers}


def test_render_mcp_passthrough_unit():
    # unit-level: a manifest declaring the file-based fields yields them on the rendered server dict
    p = Plugin.from_dict(
        {
            "id": "vault-x",
            "kind": "mcp",
            "mcp": {
                "image": "ordo/vault-x:latest",
                "transport": "http",
                "port": 9000,
                "healthcheck": _HC,
                "volumes": [_VAULT_VOLUME],
                "tools": ["read_note"],
            },
        }
    )
    servers, notes = _render_mcp([p])
    s = servers[0]
    assert s["volumes"] == [_VAULT_VOLUME]
    assert s["network"] == "internal" and s["service"] == "mcp-vault-x"
    # ordo/* project image → no pinning warning
    assert not notes
