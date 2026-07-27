"""Obsidian cross-device sync plugin (obsidian-livesync): render wiring + secret generation.

Guards the pieces that make the CouchDB LiveSync + file-bridge feature work end-to-end:
CouchDB is digest-pinned and host-portless (reached only through the edge), the bridge mirrors the
vault's notes/ folder and waits on CouchDB, live-watch polling is on (Docker Desktop binds emit no
inotify events), and the two secrets are wizard-generated (not left blank)."""
from pathlib import Path

from ordo import wizard
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.hardware import HardwareProfile
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
HW = HardwareProfile.from_spec({"gpus": [{"name": "RTX 5090", "vram_gb": 32}], "ram_gb": 128})


def _render():
    return render(Source.from_dict({"hardware": {"gpus": [{"name": "RTX 5090", "vram_gb": 32}],
                                                 "ram_gb": 128},
                                    "model": "auto", "plugins": "auto"}), CATALOG, REGISTRY)


def test_notes_services_render_with_expected_wiring():
    c = _render().compose_dict()
    svcs = c["services"]
    assert "couchdb" in svcs and "livesync-bridge" in svcs

    couch = svcs["couchdb"]
    assert "@sha256:" in couch["image"], "couchdb must be digest-pinned"
    assert "ports" not in couch, "couchdb must publish NO host port (reached via the edge /couchdb)"
    assert couch["profiles"] == ["notes"]

    bridge = svcs["livesync-bridge"]
    assert bridge["image"] == "ordo/livesync-bridge:latest"
    # live-watch on Docker Desktop bind mounts needs polling (no inotify events)
    assert bridge["environment"]["CHOKIDAR_USEPOLLING"] == "1"
    # secrets arrive via the secrets.env env_file, never ${..} interpolation (compose-config safe)
    assert any("secrets.env" in str(f) for f in bridge["env_file"]), "bridge must layer secrets.env"
    # mirrors the vault's notes/ subfolder — the ONE shared vault the MCP + RAG use
    assert any("/memory-vault}/notes:/app/data/notes" in v or "/notes:/app/data/notes" in v
               for v in bridge["volumes"]), "bridge must bind the vault notes/ folder"
    # start-order only (plugin services don't carry health conditions); the bridge's own CouchDB
    # wait-loop is the real readiness gate
    assert "couchdb" in bridge["depends_on"]


def test_notes_secrets_are_required_and_wizard_generated():
    rc = _render()
    for key in ("COUCHDB_PASSWORD", "LIVESYNC_E2EE_PASSPHRASE"):
        assert key in rc.required_secrets, f"{key} must be a required secret"

    # the wizard mints both (never left blank) — and the E2EE passphrase must be JSON-safe for the
    # bridge's generated config (base64url has no quotes/backslashes).
    values, generated, _given, blank = wizard.resolve_secrets(rc.required_secrets, {})
    assert "COUCHDB_PASSWORD" in generated and "LIVESYNC_E2EE_PASSPHRASE" in generated
    assert "COUCHDB_PASSWORD" not in blank and "LIVESYNC_E2EE_PASSPHRASE" not in blank
    for key in ("COUCHDB_PASSWORD", "LIVESYNC_E2EE_PASSPHRASE"):
        assert values[key] and '"' not in values[key] and "\\" not in values[key]


def test_notes_capability_maps_to_the_plugin():
    assert "notes" in wizard.CAPABILITIES
    assert wizard.CAPABILITIES["notes"]["plugins"] == ["obsidian-livesync"]
    # disabling notes drops exactly the plugin, nothing else
    all_ids = [p.id for p in REGISTRY.plugins]
    kept = [c for c in wizard.CAPABILITIES if c != "notes"]
    result = wizard.plugins_from_capabilities(kept, all_ids)
    assert "obsidian-livesync" not in result
