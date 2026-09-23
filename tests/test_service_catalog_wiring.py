"""Locks the dashboard↔control-plane service-control wiring and the manifest enabled-gate.

Covers two audit findings:
  1. Hermes lifecycle buttons 400 — `hermes` had no OPS_SERVICE_MAP entry and `hermes-dashboard`
     was not controllable.
  2. Service-grid drift — the catalog was hand-maintained and could silently omit an enabled
     service; the grid is now gated on the render manifest's enabled set.

The control plane's authority used to be a 25-name ALLOWED_SERVICES literal in ops-api, read out
of that file by AST parse. ops-controller replaced it with the rule that literal was approximating:
any compose service in THIS project is controllable, except the ones running the request. So the
assertions below check the rendered stack and `DockerBackend.SELF_REFERENTIAL` instead of a list
that needed an edit for every new plugin.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

from ordo.broker import DockerBackend

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard.services_catalog import (  # noqa: E402
    CARD_PLUGINS,
    OPS_SERVICE_MAP,
    SERVICES,
    _load_enabled_plugins,
    visible_services,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDERED_COMPOSE = REPO_ROOT / "out" / "docker-compose.yml"


def _rendered_services() -> set[str]:
    """Every service in the rendered stack — what the control plane will actually accept."""
    if not RENDERED_COMPOSE.exists():
        return set()
    doc = yaml.safe_load(RENDERED_COMPOSE.read_text(encoding="utf-8")) or {}
    return set(doc.get("services") or {})


# ── (a) Hermes ─────────────────────────────────────────────────────────────────

def test_hermes_card_targets_the_ui_service_not_the_gateway():
    assert OPS_SERVICE_MAP["hermes"] == "hermes-dashboard"


def test_the_agent_gateway_is_never_a_lifecycle_target():
    """Hermes reaches these verbs through its own tools, so restarting `agent` is a process
    killing itself mid-tool-call. No card may target it, and the backend refuses it outright."""
    assert "agent" not in OPS_SERVICE_MAP.values()
    assert "agent" in DockerBackend.SELF_REFERENTIAL
    with __import__("pytest").raises(ValueError):
        DockerBackend("ordo")._lifecycle_guard("agent")


def test_the_control_plane_refuses_to_cycle_itself():
    assert "ops-controller" not in OPS_SERVICE_MAP.values()
    with __import__("pytest").raises(ValueError):
        DockerBackend("ordo")._lifecycle_guard("ops-controller")


# ── (b) every controllable card is fully wired ──────────────────────────────────

def test_every_ops_mapped_service_exists_in_the_rendered_stack():
    """Each OPS_SERVICE_MAP target must be a real compose service, else the card's
    start/stop/restart buttons fail against a name the control plane cannot resolve."""
    rendered = _rendered_services()
    if not rendered:
        __import__("pytest").skip("no rendered stack in this checkout")
    for display_id, compose_id in OPS_SERVICE_MAP.items():
        assert compose_id in rendered, (
            f"{display_id} -> {compose_id} is not a service in the rendered stack"
        )


def test_no_card_targets_a_service_the_backend_refuses():
    for display_id, compose_id in OPS_SERVICE_MAP.items():
        assert compose_id not in DockerBackend.SELF_REFERENTIAL, (
            f"{display_id} -> {compose_id} is self-referential; its buttons would always fail"
        )


def test_added_cards_are_controllable():
    """The newly-added cards (plus Hermes) must each be operator-controllable."""
    for display_id in ("hermes", "stt", "tts", "rag-ingestion", "llamacpp-cpu"):
        assert display_id in OPS_SERVICE_MAP, f"{display_id} not wired for lifecycle control"


def test_missing_service_cards_added():
    ids = {s["id"] for s in SERVICES}
    for expected in ("stt", "tts", "rag-ingestion"):
        assert expected in ids


def test_catalog_ids_are_unique():
    ids = [s["id"] for s in SERVICES]
    assert len(ids) == len(set(ids))


def test_non_user_facing_services_carry_background_flag():
    """`background: True` marks NON-user-facing services — the ones the frontend moves out
    of the main grid (which is only browsable UIs) into the secondary 'Background jobs'
    section (no 'Open' link). That is the backend infra (llamacpp, qdrant, stt, tts)
    plus the two portless headless workers (rag-ingestion, livesync-bridge). The user-facing UIs —
    webui/comfyui/n8n/hermes/codebase-memory-ui — and model-gateway (its Open link is the
    LiteLLM admin UI) must NOT carry it, so an openable service can't be quietly demoted."""
    bg = {s["id"] for s in SERVICES if s.get("background")}
    assert bg == {"rag-ingestion", "llamacpp", "llamacpp-cpu", "qdrant", "stt", "tts",
                  "couchdb", "livesync-bridge"}
    user_facing = {"webui", "comfyui", "n8n", "hermes", "codebase-memory-ui", "model-gateway"}
    for s in SERVICES:
        if s["id"] in user_facing:
            assert not s.get("background"), f"{s['id']} is user-facing and must not be background"
        assert (s["id"] in bg) == bool(s.get("background")), f"{s['id']} background flag mismatch"


def test_headless_workers_have_no_ui_open_target():
    """The two pure headless workers expose no port and no health check, so the frontend has
    nothing to build a link from (and shows a neutral 'unknown' state, not a false-red). The
    other background services (llamacpp/qdrant/stt/tts) ARE probeable — they keep their
    check — they're just not user-facing, so the frontend omits their 'Open' link via the flag."""
    for wid in ("rag-ingestion", "livesync-bridge"):
        s = next(x for x in SERVICES if x["id"] == wid)
        assert s.get("port") is None, f"{wid} headless worker should have no port"
        assert s.get("check") is None, f"{wid} headless worker should have no check"


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
    assert {"llamacpp", "model-gateway"} <= visible_ids
    # Enabled plugins show.
    assert {"webui", "comfyui"} <= visible_ids
    # Disabled plugins are hidden.
    for hidden in ("qdrant", "stt", "tts", "rag-ingestion", "n8n", "hermes"):
        assert hidden not in visible_ids, f"{hidden} should be hidden when its plugin is disabled"


def test_visible_services_shows_all_for_live_enabled_set():
    """Against the real rendered manifest's enabled set, nothing is hidden."""
    enabled = {
        "automation", "codebase-memory-ui", "comfyui", "edge", "hermes-dashboard",
        "langfuse", "llamacpp-cpu", "ltx-trainer", "monitoring", "open-webui", "rag",
        "searxng-web", "song-gen", "tailnet-names", "voice", "obsidian-livesync",
        "obsidian-livesync-funnel",
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
