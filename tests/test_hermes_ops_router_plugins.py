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


NOT_JSON_TRUE = ["false", "no", "true", 1, "yes", [True]]


@pytest.mark.parametrize("confirm", NOT_JSON_TRUE)
@pytest.mark.parametrize("tool", ["_enable_service", "_disable_service"])
def test_a_plugin_tool_refuses_a_confirm_that_is_not_json_true(router, monkeypatch, tool, confirm):
    client = FakeClient({"ok": True})
    monkeypatch.setattr(router, "_get_client", lambda: client)
    result = json.loads(getattr(router, tool)({"plugin_id": "automation", "confirm": confirm}))
    assert result["ok"] is False
    assert client.calls == []


class ComposeClient:
    def __init__(self):
        self.calls = []

    def compose_restart(self, *, service, confirm):
        self.calls.append(("restart", service, confirm))
        return {"ok": True}

    def compose_up(self, *, service, confirm):
        self.calls.append(("up", service, confirm))
        return {"ok": True}


@pytest.mark.parametrize("confirm", NOT_JSON_TRUE)
@pytest.mark.parametrize("tool, verb", [("_compose_restart", "restart"), ("_compose_up", "up")])
def test_a_compose_tool_forwards_only_json_true_as_a_confirmation(router, monkeypatch, tool, verb, confirm):
    client = ComposeClient()
    monkeypatch.setattr(router, "_get_client", lambda: client)
    getattr(router, tool)({"service": "n8n", "confirm": confirm})
    assert client.calls == [(verb, "n8n", False)]


@pytest.mark.parametrize("tool", ["_compose_restart", "_compose_up"])
def test_a_compose_tool_refuses_a_stack_wide_call_on_a_string_confirm(router, monkeypatch, tool):
    client = ComposeClient()
    monkeypatch.setattr(router, "_get_client", lambda: client)
    result = json.loads(getattr(router, tool)({"confirm": "false"}))
    assert result["ok"] is False
    assert client.calls == []


class RestartClient:
    def __init__(self):
        self.calls = []

    def restart_container(self, name, *, confirm=False):
        self.calls.append((name, confirm))
        return {"ok": True, "container": name}


def test_restart_container_forwards_confirm_true(router, monkeypatch):
    client = RestartClient()
    monkeypatch.setattr(router, "_get_client", lambda: client)
    result = json.loads(router._restart_container({"name": "ordo-n8n-1", "confirm": True}))
    assert result["ok"] is True
    assert client.calls == [("ordo-n8n-1", True)]


@pytest.mark.parametrize("args", [{"name": "ordo-n8n-1"}, *({"name": "ordo-n8n-1", "confirm": c} for c in NOT_JSON_TRUE)])
def test_restart_container_refuses_without_json_true(router, monkeypatch, args):
    client = RestartClient()
    monkeypatch.setattr(router, "_get_client", lambda: client)
    result = json.loads(router._restart_container(args))
    assert result["ok"] is False and "confirm" in result["error"]
    assert client.calls == []


def test_restart_container_schema_requires_confirm(router):
    parameters = router.RESTART_CONTAINER_SCHEMA["parameters"]
    assert parameters["properties"]["confirm"]["type"] == "boolean"
    assert "confirm" in parameters["required"]


# --- The tool text is what the model routes on, so it must match what ops-controller does. ---
# ops-controller's container verbs act only on the `ordo` compose project (DockerBackend's
# _container_guard), and compose_restart / compose_up both call POST /services/{service}/recreate.
# The old text promised logs and restarts for ANY host container and told the model that raw
# `docker` "also works": each refusal then sent it to the socket (hostile audit SEC-1, 2026-09-29).

def _tool_text(router) -> str:
    schemas = [router.LIST_CONTAINERS_SCHEMA, router.CONTAINER_LOGS_SCHEMA, router.INSPECT_CONTAINER_SCHEMA,
               router.RESTART_CONTAINER_SCHEMA,
               router.COMPOSE_RESTART_SCHEMA, router.COMPOSE_UP_SCHEMA]
    return json.dumps(schemas) + router._NUDGE + (router.__doc__ or "")


def test_no_tool_text_promises_a_container_outside_the_ordo_project(router):
    text = _tool_text(router).lower()
    assert "any container" not in text
    assert "non-ordo" not in text


def test_no_tool_text_points_the_model_at_raw_docker(router):
    text = _tool_text(router).lower()
    assert "also works" not in text
    assert "docker socket" not in text


@pytest.mark.parametrize("schema", ["CONTAINER_LOGS_SCHEMA", "RESTART_CONTAINER_SCHEMA"])
def test_the_container_verbs_say_they_are_ordo_only(router, schema):
    assert "ordo project" in getattr(router, schema)["description"].lower()


def test_compose_restart_says_it_recreates(router):
    description = router.COMPOSE_RESTART_SCHEMA["description"].lower()
    assert "/services/{service}/recreate" in description
    assert "does not recreate" not in description
    assert "compose_restart` only bounce" not in router._NUDGE


@pytest.mark.parametrize("schema", ["COMPOSE_RESTART_SCHEMA", "COMPOSE_UP_SCHEMA"])
def test_a_compose_tool_schema_requires_a_service(router, schema):
    parameters = getattr(router, schema)["parameters"]
    assert parameters["required"] == ["service", "confirm"]
    assert "whole stack" not in json.dumps(parameters).lower()


def test_the_seed_soul_retires_raw_docker():
    soul = (HERMES / "seed" / "SOUL.md").read_text(encoding="utf-8")
    assert "docker socket IS mounted" not in soul
    assert "tail any container" not in soul
    assert "restart any container" not in soul
    for tool in ("list_containers", "container_logs", "restart_container", "compose_up"):
        assert tool in soul


# --- inspect_container: the read-only replacement for `docker inspect` (SEC-1 step 3) ---

class InspectClient:
    def __init__(self):
        self.calls = []

    def inspect_container(self, name):
        self.calls.append(name)
        return {"name": name, "state": "running", "health": "healthy"}


def test_inspect_container_returns_the_summary(router, monkeypatch):
    client = InspectClient()
    monkeypatch.setattr(router, "_get_client", lambda: client)
    result = json.loads(router._inspect_container({"name": "ordo-n8n-1"}))
    assert result == {"ok": True, "container": {"name": "ordo-n8n-1", "state": "running", "health": "healthy"}}
    assert client.calls == ["ordo-n8n-1"]


def test_inspect_container_needs_a_name(router, monkeypatch):
    client = InspectClient()
    monkeypatch.setattr(router, "_get_client", lambda: client)
    assert json.loads(router._inspect_container({}))["ok"] is False
    assert client.calls == []


def test_inspect_container_says_what_it_returns(router):
    schema = router.INSPECT_CONTAINER_SCHEMA
    text = schema["description"].lower()
    assert "ordo project" in text and "read-only" in text and "environment" in text
    assert schema["parameters"]["required"] == ["name"]


def test_the_client_calls_the_inspect_route(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("ops_client_under_test", HERMES / "ops_client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "t")
    client = module.OpsClient(url="http://ops-controller:9000")
    seen = []

    class Response:
        status_code = 200

        def json(self):
            return {"name": "ordo-n8n-1"}

    monkeypatch.setattr(client._client, "request", lambda method, path, **kw: seen.append((method, path)) or Response())
    assert client.inspect_container("ordo-n8n-1") == {"name": "ordo-n8n-1"}
    assert seen == [("GET", "/containers/ordo-n8n-1")]
