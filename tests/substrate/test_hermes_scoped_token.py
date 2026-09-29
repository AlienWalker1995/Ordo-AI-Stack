"""Hermes presents its own scoped token to ops-controller and cannot read the admin one (SEC-1 step 6).

- The agent's ops-controller credential is OPS_CONTROLLER_TOKEN_HERMES (the `hermes` principal,
  ordo/control/principals.py), delivered as a file at /run/secrets/ops_controller_token_hermes.
  Inside the agent it is still named OPS_CONTROLLER_TOKEN (the entrypoint bridges
  OPS_CONTROLLER_TOKEN_FILE), so every tool, script and skill that already presents
  $OPS_CONTROLLER_TOKEN now presents the scoped token. The admin token never reaches the agent.
- The agent mirror-mounts the operator's code root at /c/dev, which holds the materialized secret
  store (`out/secrets.env`, `out/secrets/`). Those two paths are shadowed inside the agent: an
  empty read-only volume over the directory and /dev/null over the file.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from ordo.control import principals
from ordo.render import agent_mirror
from ordo.render.agents import AgentRegistry
from ordo.render.catalog import Catalog
from ordo.render.config import Source
from ordo.render.engine import render
from ordo.render.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parents[2]
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "services")
AGENTS = AgentRegistry.load(ROOT / "services")
HERMES = ROOT / "services" / "hermes"
SITE = {"BASE_PATH": "C:/dev/ordo-ai-stack", "CODE_ROOT": "C:/dev", "DATA_PATH": "C:/dev/ordo-ai-stack/data"}


def _rendered(tmp_path, site=SITE):
    rc = render(Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto",
                                  "plugins": "auto", "agent": "hermes", "site": site}),
                CATALOG, REGISTRY, agents=AGENTS)
    rc.write(tmp_path)
    compose = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    env = dict(line.split("=", 1) for line in (tmp_path / ".env").read_text().splitlines()
               if line and not line.startswith("#") and "=" in line)
    return rc, compose, env


# --------------------------------------------------------------------------- #
# the credential
# --------------------------------------------------------------------------- #

def test_the_agent_reads_the_scoped_token_from_a_file(tmp_path):
    _, compose, _ = _rendered(tmp_path)
    agent = compose["services"]["agent"]
    assert agent["environment"]["OPS_CONTROLLER_TOKEN_FILE"] == "/run/secrets/ops_controller_token_hermes"
    assert any(v.endswith("/out/secrets/ops_controller_token_hermes:/run/secrets/ops_controller_token_hermes:ro")
               for v in agent["volumes"])


def test_the_admin_token_never_reaches_the_agent(tmp_path):
    _, compose, _ = _rendered(tmp_path)
    agent = compose["services"]["agent"]
    assert "OPS_CONTROLLER_TOKEN" not in agent["environment"]
    assert "${OPS_CONTROLLER_TOKEN}" not in json.dumps(agent)
    assert not any("/out/secrets/ops_controller_token:" in v for v in agent["volumes"])


def test_the_scoped_token_is_now_required(tmp_path):
    rc, _, _ = _rendered(tmp_path)
    assert "OPS_CONTROLLER_TOKEN_HERMES" in rc.required_secrets
    assert "OPS_CONTROLLER_TOKEN_HERMES" not in rc.optional_secrets


def test_the_entrypoint_bridges_the_token_file():
    text = (HERMES / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'if [ -n "${OPS_CONTROLLER_TOKEN_FILE:-}" ] && [ -s "$OPS_CONTROLLER_TOKEN_FILE" ]; then' in text
    assert 'OPS_CONTROLLER_TOKEN="$(cat "$OPS_CONTROLLER_TOKEN_FILE")"' in text


def _ops_client():
    spec = importlib.util.spec_from_file_location("ops_client_scoped", HERMES / "ops_client.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_route_the_agent_calls_is_granted_to_the_hermes_principal(monkeypatch):
    """The switch must not cut Hermes off from its own tools: every OpsClient call (the ops-router
    tools) and every tracked Hermes script's route is in HERMES_ROUTES."""
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN", "t")
    module = _ops_client()
    client = module.OpsClient(url="http://ops-controller:9000")
    seen = []

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {}

    monkeypatch.setattr(client._client, "request", lambda method, path, **kw: seen.append((method, path)) or Response())
    client.list_containers()
    client.container_logs("ordo-n8n-1")
    client.inspect_container("ordo-n8n-1")
    client.restart_container("ordo-n8n-1", confirm=True)
    client.compose_up(service="n8n", confirm=True)
    client.compose_restart(service="n8n", confirm=True)
    client.list_plugins()
    client.enable_plugin("automation", confirm=True)
    client.disable_plugin("automation", confirm=True)
    client.list_projects()
    client.project_containers("nas-stack")
    client.project_logs("nas-stack", "janitorr")
    client.restart_project_container("nas-stack", "janitorr", confirm=True)
    # scripts/stack_monitor.py and scripts/hermes/comfyui_idle_reclaim.sh
    seen += [("GET", "/diagnostics/dstate"), ("GET", "/stats/services"), ("GET", "/status"),
             ("POST", "/services/comfyui/restart")]
    hermes = principals.hermes(lambda: "t")
    assert [call for call in seen if not hermes.allows(*call)] == []


# --------------------------------------------------------------------------- #
# the shadows over the materialized secret store
# --------------------------------------------------------------------------- #

def test_the_mirror_root_matches_the_agent_manifest():
    manifest = yaml.safe_load((HERMES / "agent.yaml").read_text(encoding="utf-8"))
    assert f"${{CODE_ROOT:-{agent_mirror.MIRROR_ROOT}}}:{agent_mirror.MIRROR_ROOT}" in manifest["volumes"]


@pytest.mark.parametrize("base_path, code_root, expected", [
    ("C:/dev/ordo-ai-stack", "C:/dev", "/c/dev/ordo-ai-stack"),
    ("c:\\dev\\ordo-ai-stack", "C:\\dev\\", "/c/dev/ordo-ai-stack"),
    ("/c/dev/ordo-ai-stack", "C:/dev", "/c/dev/ordo-ai-stack"),
    ("/srv/code/ordo", "/srv/code", "/c/dev/ordo"),
    ("/c/dev/ordo-ai-stack", "", "/c/dev/ordo-ai-stack"),          # CODE_ROOT unset: the /c/dev default
])
def test_the_checkout_path_inside_the_agent(base_path, code_root, expected):
    assert agent_mirror.checkout_in_agent(base_path, code_root) == expected


@pytest.mark.parametrize("base_path, code_root", [
    ("D:/elsewhere/ordo-ai-stack", "C:/dev"),
    ("/opt/ordo", "/srv/code"),
    ("C:/dev-other/ordo", "C:/dev"),                                   # a prefix is not a parent
    ("", "C:/dev"),
])
def test_a_checkout_outside_the_mirror_is_not_visible(base_path, code_root):
    assert agent_mirror.checkout_in_agent(base_path, code_root) == agent_mirror.NOT_MIRRORED


def test_the_render_hides_the_secret_store_from_the_agent(tmp_path):
    _, compose, env = _rendered(tmp_path)
    volumes = compose["services"]["agent"]["volumes"]
    assert env["AGENT_CHECKOUT_PATH"] == "/c/dev/ordo-ai-stack"
    assert "hermes-secret-shadow:${AGENT_CHECKOUT_PATH}/out/secrets:ro" in volumes
    assert "/dev/null:${AGENT_CHECKOUT_PATH}/out/secrets.env:ro" in volumes
    assert "hermes-secret-shadow" in compose["volumes"]
    # the shadows come after the mirror mount they cover (compose orders parents first anyway)
    mirror = next(i for i, v in enumerate(volumes) if v.endswith(":/c/dev"))
    assert all(i > mirror for i, v in enumerate(volumes) if "AGENT_CHECKOUT_PATH" in v)


def test_the_checkout_path_is_derived_and_cannot_be_shadowed_by_site(tmp_path):
    _, _, env = _rendered(tmp_path, {**SITE, "AGENT_CHECKOUT_PATH": "/tmp/elsewhere"})
    assert env["AGENT_CHECKOUT_PATH"] == "/c/dev/ordo-ai-stack"


# --------------------------------------------------------------------------- #
# the prompt
# --------------------------------------------------------------------------- #

def test_the_seed_soul_explains_the_scoped_token():
    soul = (HERMES / "seed" / "SOUL.md").read_text(encoding="utf-8")
    assert "scoped" in soul and "403" in soul
    assert "secrets.env" in soul        # it says not to go looking for another token
