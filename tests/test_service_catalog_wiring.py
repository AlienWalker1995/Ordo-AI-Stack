"""Locks the dashboard↔ops-api service-control wiring and the manifest enabled-gate.

Covers two audit findings:
  1. Hermes lifecycle buttons 400 — `hermes` had no OPS_SERVICE_MAP entry and
     `hermes-dashboard` was not in ops-api's ALLOWED_SERVICES.
  2. Service-grid drift — the catalog was hand-maintained and could silently omit
     an enabled service; the grid is now gated on the render manifest's enabled set.

ALLOWED_SERVICES is extracted from ops-api/main.py by AST parse (NOT import) so this
dashboard test never pulls in docker/fastapi or the module's startup side effects.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard.services_catalog import (  # noqa: E402
    CARD_PLUGINS,
    OPS_SERVICE_MAP,
    SERVICES,
    _load_enabled_plugins,
    visible_services,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OPS_API_MAIN = REPO_ROOT / "docker" / "ops-api" / "main.py"


def _ops_api_allowed_services() -> set[str]:
    """Extract the ALLOWED_SERVICES set literal from ops-api/main.py without importing it."""
    tree = ast.parse(OPS_API_MAIN.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "ALLOWED_SERVICES":
                    return set(ast.literal_eval(node.value))
    raise AssertionError("ALLOWED_SERVICES not found in ops-api/main.py")


ALLOWED_SERVICES = _ops_api_allowed_services()


# ── (a) Hermes ─────────────────────────────────────────────────────────────────

def test_hermes_maps_to_dashboard_service_and_is_allowlisted():
    assert OPS_SERVICE_MAP["hermes"] == "hermes-dashboard"
    assert "hermes-dashboard" in ALLOWED_SERVICES


def test_agent_gateway_is_not_allowlisted():
    """The Hermes agent/gateway self-restart is delicate — it must NOT be controllable."""
    assert "agent" not in ALLOWED_SERVICES
    assert "hermes" not in OPS_SERVICE_MAP.values()  # never target the gateway directly


# ── (b) every controllable card is fully wired ──────────────────────────────────

def test_every_ops_mapped_service_is_allowlisted():
    """Each OPS_SERVICE_MAP target must be an allowlisted compose service name, else
    the card's start/stop/restart buttons 400 in ops-api."""
    for display_id, compose_id in OPS_SERVICE_MAP.items():
        assert compose_id in ALLOWED_SERVICES, (
            f"{display_id} -> {compose_id} missing from ops-api ALLOWED_SERVICES"
        )


def test_added_cards_are_controllable():
    """The newly-added cards (plus Hermes) must each be operator-controllable."""
    for display_id in ("hermes", "stt", "tts", "worker", "rag-ingestion"):
        assert display_id in OPS_SERVICE_MAP, f"{display_id} not wired for lifecycle control"


def test_missing_service_cards_added():
    ids = {s["id"] for s in SERVICES}
    for expected in ("stt", "tts", "worker", "rag-ingestion"):
        assert expected in ids


def test_catalog_ids_are_unique():
    ids = [s["id"] for s in SERVICES]
    assert len(ids) == len(set(ids))


def test_only_headless_workers_carry_background_flag():
    """worker + rag-ingestion are the pure no-UI background jobs; ONLY they carry
    `background: True` (the marker the frontend uses to split them into a separate
    'Background jobs' section). Every other card stays an interactive service — so a
    stray flag can't quietly demote an openable service out of the main grid."""
    bg = {s["id"] for s in SERVICES if s.get("background")}
    assert bg == {"worker", "rag-ingestion"}
    for s in SERVICES:
        if s["id"] not in bg:
            assert not s.get("background"), f"{s['id']} unexpectedly flagged background"


def test_background_jobs_have_no_ui_open_target():
    """Background jobs are headless: no port and no health check, so the frontend has
    nothing to build an 'Open' link from (and shows a neutral, not false-red, state)."""
    for s in SERVICES:
        if s.get("background"):
            assert s.get("port") is None, f"{s['id']} background job should have no port"
            assert s.get("check") is None, f"{s['id']} background job should have no check"


def test_every_card_declares_plugin_gate():
    """Every entry must carry an explicit `plugin` key (None for core) so the
    enabled-gate is total — a forgotten key would make a card ungate-able."""
    for s in SERVICES:
        assert "plugin" in s, f"{s['id']} is missing the plugin gate key"


# ── (c) manifest enabled-gate helper ────────────────────────────────────────────

def test_visible_services_hides_disabled_plugins():
    enabled = {"open-webui", "comfyui"}  # rag / voice / automation / hermes disabled
    visible_ids = {s["id"] for s in visible_services(enabled=enabled)}
    # Core services (plugin=None) always show.
    assert {"llamacpp", "model-gateway", "mcp"} <= visible_ids
    # Enabled plugins show.
    assert {"webui", "comfyui"} <= visible_ids
    # Disabled plugins are hidden.
    for hidden in ("qdrant", "stt", "tts", "worker", "rag-ingestion", "n8n", "hermes"):
        assert hidden not in visible_ids, f"{hidden} should be hidden when its plugin is disabled"


def test_visible_services_shows_all_for_live_enabled_set():
    """Against the real rendered manifest's enabled set, nothing is hidden."""
    enabled = {
        "automation", "codebase-memory-ui", "comfyui", "edge", "hermes-dashboard",
        "ltx-trainer", "monitoring", "open-webui", "rag", "searxng-web", "song-gen",
        "tailnet-names", "voice", "worker",
    }
    visible_ids = {s["id"] for s in visible_services(enabled=enabled)}
    assert visible_ids == {s["id"] for s in SERVICES}


def test_visible_services_fails_open_when_manifest_absent(monkeypatch):
    monkeypatch.delenv("MANIFEST_PATH", raising=False)
    assert visible_services() == list(SERVICES)


def test_visible_services_reads_manifest_path(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"plugins_enabled": ["open-webui"]}), encoding="utf-8")
    monkeypatch.setenv("MANIFEST_PATH", str(manifest))
    assert _load_enabled_plugins() == {"open-webui"}
    visible_ids = {s["id"] for s in visible_services()}
    assert "webui" in visible_ids
    assert "comfyui" not in visible_ids


def test_load_enabled_plugins_none_on_missing_or_malformed(tmp_path, monkeypatch):
    # Unset -> None (fail open)
    monkeypatch.delenv("MANIFEST_PATH", raising=False)
    assert _load_enabled_plugins() is None
    # Missing file -> None
    monkeypatch.setenv("MANIFEST_PATH", str(tmp_path / "nope.json"))
    assert _load_enabled_plugins() is None
    # Malformed (no plugins_enabled list) -> None
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"model": {}}), encoding="utf-8")
    monkeypatch.setenv("MANIFEST_PATH", str(bad))
    assert _load_enabled_plugins() is None


def test_drift_guard_warns_on_uncovered_enabled_plugin(caplog):
    """If a card-bearing plugin is enabled but no catalog entry covers it, warn."""
    catalog = [s for s in SERVICES if s.get("plugin") != "voice"]  # drop stt + tts cards
    with caplog.at_level("WARNING"):
        visible_services(services=catalog, enabled={"voice"})
    assert any("voice" in rec.getMessage() for rec in caplog.records), \
        "expected a drift warning naming the uncovered 'voice' plugin"
    assert "voice" in CARD_PLUGINS  # sanity: voice is a tracked card-bearing plugin
