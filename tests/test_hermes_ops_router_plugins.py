"""Hermes' enable_service / disable_service tools report ops-controller's apply; they recreate nothing.

ops-controller writes the source, re-renders and recreates exactly what the render changed
(ordo/control/api.py apply_render). The tools used to bring each service up (or down) themselves
afterwards, a second, hand-written idea of what must restart. The plugin is loaded the way the
image assembles it (services/hermes/Dockerfile copies ops_client.py next to it).
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

HERMES = Path(__file__).resolve().parents[1] / "services" / "hermes"
APPLIED = {"recreated": ["n8n", "model-gateway"], "stopped": [], "restart_required_on_host": ["agent"],
           "host_reasons": {"agent": "agent runs the control plane"}, "host_command": "ordo apply --only agent"}


@pytest.fixture
def router(tmp_path, monkeypatch):
    package = tmp_path / "ops_router_under_test"
    package.mkdir()
    shutil.copy(HERMES / "plugins" / "ops-router" / "__init__.py", package / "__init__.py")
    shutil.copy(HERMES / "ops_client.py", package / "ops_client.py")
    spec = importlib.util.spec_from_file_location("ops_router_under_test", package / "__init__.py",
                                                  submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "ops_router_under_test", module)
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def enable_plugin(self, plugin_id, *, confirm=False):
        self.calls.append(("enable", plugin_id))
        return self.reply

    def disable_plugin(self, plugin_id, *, confirm=False):
        self.calls.append(("disable", plugin_id))
        return self.reply

    def __getattr__(self, name):  # compose_up / compose_down / anything else: must not be called
        raise AssertionError(f"the tool called {name}; ops-controller already applied the render")


def test_enable_reports_what_ops_controller_applied(router, monkeypatch):
    client = FakeClient({"ok": True, "already_rendered": False, "services": ["n8n"], "missing_secrets": [],
                         "warnings": [], "apply": APPLIED})
    monkeypatch.setattr(router, "_get_client", lambda: client)
    result = json.loads(router._enable_service({"plugin_id": "automation", "confirm": True}))
    assert client.calls == [("enable", "automation")]
    assert result["ok"] is True
    assert result["recreated"] == ["n8n", "model-gateway"]
    assert result["restart_required_on_host"] == ["agent"]
    assert result["host_command"] == "ordo apply --only agent"


def test_disable_reports_what_ops_controller_stopped(router, monkeypatch):
    client = FakeClient({"ok": True, "plugin": "automation",
                         "apply": {**APPLIED, "recreated": [], "stopped": ["n8n"], "restart_required_on_host": [],
                                   "host_reasons": {}, "host_command": None}})
    monkeypatch.setattr(router, "_get_client", lambda: client)
    result = json.loads(router._disable_service({"plugin_id": "automation", "confirm": True}))
    assert client.calls == [("disable", "automation")]
    assert result["stopped"] == ["n8n"] and result["host_command"] is None
