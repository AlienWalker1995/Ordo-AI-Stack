"""Dashboard MCP persistence: a UI enable/disable is persisted by ops-controller, the single writer of
the operator source. The dashboard maps the toggled server id to its kind=mcp plugin and calls
`POST /plugins/{plugin}/enable|disable`, which edits ordo.yaml surgically (ordo/render/source_edit.py) and
re-renders out/ in one step. The dashboard never reads or writes ordo.yaml itself."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastapi", reason="dashboard runtime deps (fastapi) not present")

import dashboard.app as dashboard_app  # noqa: E402
from dashboard.app import _persist_mcp_toggle  # noqa: E402

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "services" / "dashboard"
PLUGIN_MAP = {"comfyui": "comfyui-mcp", "searxng": "searxng"}


def _persist(monkeypatch, server, action, ops_reply):
    ops = AsyncMock(return_value=ops_reply)
    monkeypatch.setattr("dashboard.app._ops_request", ops)
    monkeypatch.setattr("dashboard.app._read_server_plugin_map", lambda: PLUGIN_MAP)
    return asyncio.run(_persist_mcp_toggle(server, action)), ops


APPLIED = {"recreated": ["mcp-comfyui", "model-gateway", "model-gateway-keys"], "stopped": [],
           "restart_required_on_host": [], "host_command": None}


def test_enable_calls_ops_plugin_enable_for_the_mapped_plugin(monkeypatch):
    res, ops = _persist(monkeypatch, "comfyui", "add",
                        (200, {"ok": True, "plugin": "comfyui-mcp", "apply": APPLIED}))
    assert ops.await_count == 1
    method, path = ops.await_args.args
    assert (method, path) == ("POST", "/plugins/comfyui-mcp/enable")
    # ops-controller applied the render itself: the toggle reports what it recreated.
    assert res == {"persistent": True, "plugin": "comfyui-mcp", "note": None,
                   "recreated": ["mcp-comfyui", "model-gateway", "model-gateway-keys"], "stopped": [],
                   "restart_required_on_host": [], "host_command": None}


def test_disable_calls_ops_plugin_disable_for_the_mapped_plugin(monkeypatch):
    res, ops = _persist(monkeypatch, "searxng", "remove", (200, {"ok": True, "plugin": "searxng", "apply": {
        "recreated": ["model-gateway", "model-gateway-keys"], "stopped": ["mcp-searxng"],
        "restart_required_on_host": [], "host_command": None}}))
    assert ops.await_args.args == ("POST", "/plugins/searxng/disable")
    assert res["persistent"] is True and res["plugin"] == "searxng"
    assert res["stopped"] == ["mcp-searxng"] and res["recreated"] == ["model-gateway", "model-gateway-keys"]


def test_a_render_ops_controller_did_not_apply_is_not_reported_as_applied(monkeypatch):
    # An ops-controller older than the post-render step writes the source and stops there.
    res, _ = _persist(monkeypatch, "comfyui", "add", (200, {"ok": True, "plugin": "comfyui-mcp"}))
    assert res["persistent"] is True and res["recreated"] == []
    assert "ordo apply" in res["note"]


def test_server_not_in_map_is_flagged_without_calling_ops(monkeypatch):
    res, ops = _persist(monkeypatch, "some-random-docker-mcp", "add", (200, {"ok": True}))
    assert ops.await_count == 0
    assert res["persistent"] is False and res["plugin"] is None and "out of scope" in res["note"]


def test_ops_refusal_is_not_persistent_and_carries_the_reason(monkeypatch):
    res, _ = _persist(monkeypatch, "comfyui", "add",
                      (422, {"error": "cannot safely edit ordo.yaml plugins list: inline list"}))
    assert res["persistent"] is False and res["plugin"] == "comfyui-mcp"
    assert "inline list" in res["note"]


def test_ops_unreachable_is_not_persistent(monkeypatch):
    res, _ = _persist(monkeypatch, "comfyui", "remove", (503, {"detail": "OPS_CONTROLLER_TOKEN not configured"}))
    assert res["persistent"] is False and "OPS_CONTROLLER_TOKEN" in res["note"]


def test_a_disable_under_plugins_auto_is_refused_and_not_persistent(monkeypatch):
    res, _ = _persist(monkeypatch, "comfyui", "remove",
                      (409, {"error": "ordo.yaml has `plugins: auto` ... without an explicit `plugins:` list"}))
    assert res["persistent"] is False and "explicit `plugins:` list" in res["note"]
    assert res["recreated"] == [] and res["host_command"] is None


def test_dashboard_has_no_writer_of_the_operator_source():
    # the private copy of the plugins editor and the source path are gone
    assert not hasattr(dashboard_app, "_edit_plugins_list")
    assert not hasattr(dashboard_app, "ORDO_SOURCE_PATH")
    sources = "\n".join(p.read_text(encoding="utf-8") for p in (DASHBOARD_DIR / "dashboard").glob("*.py"))
    assert "ORDO_SOURCE_PATH" not in sources and "/ordo-source.yaml" not in sources
    # and the dashboard no longer mounts out/ordo.yaml at all
    manifest = (DASHBOARD_DIR / "dashboard.yaml").read_text(encoding="utf-8")
    assert "out/ordo.yaml" not in manifest and "ORDO_SOURCE_PATH" not in manifest
