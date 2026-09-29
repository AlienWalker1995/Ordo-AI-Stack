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
import re
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


# Every plugin whose mounts or credentials matter here.
PLUGINS = ["comfyui", "comfyui-mcp", "orchestration", "evals", "hermes-dashboard", "edge", "monitoring",
           "langfuse", "searxng-web", "open-webui", "rag", "automation", "voice", "searxng", "memory-vault"]


def _rendered(tmp_path, site=SITE, **extra):
    rc = render(Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto",
                                  "plugins": "auto", "agent": "hermes", "site": site, **extra}),
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



# --------------------------------------------------------------------------- #
# Review of #310. F1: out/ is read-only to the agent (ops-controller composes from it).
# F2: every path another service executes or reads config from is read-only to the agent.
# --------------------------------------------------------------------------- #

_ROOT_VAR = re.compile(r"^\$\{(BASE_PATH|DATA_PATH)(?::[?-][^}]*)?\}(/[^:]*)?:")


def _explicit_plugins_render(tmp_path, **extra):
    site = {**SITE, "CADDY_BIND": "127.0.0.1", "CADDY_TAILNET_HOSTNAME": "host.example.ts.net",
            "CADDY_TAILNET_DOMAIN": "example.ts.net",
            "MEMORY_VAULT_PATH": "C:/dev/ordo-ai-stack/data/memory-vault", "SSO_ALLOWED_EMAILS": "a@example.com"}
    rc = render(Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto",
                                  "plugins": PLUGINS, "agent": "hermes", "site": site, **extra}),
                CATALOG, REGISTRY, agents=AGENTS)
    return rc, rc.compose_dict()


def test_out_is_read_only_inside_the_agent(tmp_path):
    _, compose = _explicit_plugins_render(tmp_path)
    assert "${BASE_PATH:?BASE_PATH must be set (non-empty)}/out:${AGENT_CHECKOUT_PATH}/out:ro" \
        in compose["services"]["agent"]["volumes"]


def test_every_checkout_path_another_service_mounts_is_read_only_to_the_agent(tmp_path):
    """Derived from the render, so a new plugin's config or code mount is covered without a list."""
    _, compose = _explicit_plugins_render(tmp_path)
    agent = compose["services"]["agent"]["volumes"]
    wanted = set()
    for name, svc in compose["services"].items():
        if name == "agent":
            continue
        for vol in svc.get("volumes") or []:
            m = _ROOT_VAR.match(vol) if isinstance(vol, str) else None
            if m and m.group(1) == "BASE_PATH" and not (m.group(2) or "").startswith("/out"):
                wanted.add(m.group(2))
    assert {"/scripts/comfyui", "/scripts/llamacpp", "/auth/caddy/Caddyfile", "/services/evals"} <= wanted
    for rel in wanted:
        assert f"${{BASE_PATH:?BASE_PATH must be set (non-empty)}}{rel}:${{AGENT_CHECKOUT_PATH}}{rel}:ro" in agent, rel


def test_the_control_planes_state_is_read_only_to_the_agent(tmp_path):
    """ops-controller's audit log and saved lease state live in data/ops-controller, which the
    agent sees twice: at /workspace/data and through the /c/dev mirror."""
    _, compose = _explicit_plugins_render(tmp_path)
    agent = compose["services"]["agent"]["volumes"]
    src = "${DATA_PATH:?DATA_PATH must be set (non-empty)}/ops-controller"
    assert f"{src}:/workspace/data/ops-controller:ro" in agent
    assert f"{src}:${{AGENT_DATA_PATH}}/ops-controller:ro" in agent


def test_the_data_path_inside_the_agent_is_derived(tmp_path):
    _, _, env = _rendered(tmp_path)
    assert env["AGENT_DATA_PATH"] == "/c/dev/ordo-ai-stack/data"


def test_declared_foreign_paths_are_read_only_to_the_agent(tmp_path):
    """Paths other projects execute (nas-stack's custom-cont-init.d) cannot be derived from Ordo's
    render: the operator declares them in ordo.yaml `agent_readonly:`."""
    _, compose = _explicit_plugins_render(tmp_path, agent_readonly=["C:/dev/nas-stack/jellyfin/custom-cont-init.d"])
    assert "C:/dev/nas-stack/jellyfin/custom-cont-init.d:/c/dev/nas-stack/jellyfin/custom-cont-init.d:ro" \
        in compose["services"]["agent"]["volumes"]


@pytest.mark.parametrize("paths, why", [
    ("C:/dev/x", "list"),
    (["relative/path"], "absolute"),
    (["D:/elsewhere/x"], "CODE_ROOT"),
    (["C:/dev/a", "C:/dev/a"], "twice"),
])
def test_a_bad_agent_readonly_list_is_refused(paths, why):
    with pytest.raises(ValueError, match=why):
        render(Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128}, "model": "auto",
                                 "plugins": "auto", "agent": "hermes", "site": SITE, "agent_readonly": paths}),
               CATALOG, REGISTRY, agents=AGENTS)


# --------------------------------------------------------------------------- #
# F2: admin-token holders the agent can reach
# --------------------------------------------------------------------------- #

def test_comfyui_holds_no_ops_controller_token(tmp_path):
    """ComfyUI's only reader was a retired no-op node parameter; it runs code from the checkout."""
    _, compose = _explicit_plugins_render(tmp_path)
    assert "OPS_CONTROLLER_TOKEN" not in json.dumps(compose["services"]["comfyui"])


def test_the_hermes_dashboard_presents_the_scoped_token(tmp_path):
    """Same image and brain volume as the agent: it gets the same scoped credential, never the admin."""
    _, compose = _explicit_plugins_render(tmp_path)
    dash = compose["services"]["hermes-dashboard"]
    assert dash["environment"]["OPS_CONTROLLER_TOKEN_FILE"] == "/run/secrets/ops_controller_token_hermes"
    assert "OPS_CONTROLLER_TOKEN" not in dash["environment"]
    assert "${OPS_CONTROLLER_TOKEN}" not in json.dumps(dash)


# --------------------------------------------------------------------------- #
# F3: MCP servers that call ops-controller with the admin token are not in Hermes' key
# --------------------------------------------------------------------------- #

def _grant(rc, alias):
    return next(k for k in rc.litellm_keys if k["alias"] == alias)["mcp_servers"]


def test_hermes_key_excludes_the_admin_powered_mcp_servers(tmp_path):
    rc, _ = _explicit_plugins_render(tmp_path)
    granted = _grant(rc, "hermes")
    assert "comfyui" not in granted and "orchestration" not in granted
    assert {"searxng", "memory_vault"} <= set(granted)                         # the rest is still granted


def test_all_except_names_must_be_known_servers():
    from ordo.render.engine import render_litellm_keys
    with pytest.raises(ValueError, match="unknown MCP servers"):
        render_litellm_keys([("x", {"models": ["local-chat"], "mcp_servers": {"all_except": ["comfyiu"]}})],
                            ["comfyui", "searxng"], model_names=["local-chat"], all_server_names=["comfyui", "searxng"])


def test_all_except_may_name_a_disabled_server():
    from ordo.render.engine import render_litellm_keys
    [key] = render_litellm_keys([("x", {"models": ["local-chat"], "mcp_servers": {"all_except": ["orchestration"]}})],
                                ["searxng"], model_names=["local-chat"], all_server_names=["searxng", "orchestration"])
    assert key["mcp_servers"] == ["searxng"]


# --------------------------------------------------------------------------- #
# F4, F5: the skills Hermes loads for ops work, shipped read-only in the image
# --------------------------------------------------------------------------- #

SHIPPED = HERMES / "skills"


def _frontmatter_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---", 2)[1])["name"]


def test_the_ops_skills_are_shipped_and_copied_into_the_image():
    names = {_frontmatter_name(p) for p in SHIPPED.rglob("SKILL.md")}
    assert {"ops-controller-api", "stack-image-updates", "docker-isolated-deployment"} <= names
    assert "COPY skills/ /opt/ordo-skills/" in (HERMES / "Dockerfile").read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", sorted(SHIPPED.rglob("SKILL.md")), ids=lambda p: p.parent.name)
def test_no_shipped_skill_teaches_a_refused_route_or_the_secret_store(skill):
    text = skill.read_text(encoding="utf-8")
    hermes = principals.hermes(lambda: "t")
    for method, path in re.findall(r"\b(GET|POST) (/[A-Za-z0-9_/{}.-]+)", text):
        concrete = re.sub(r"\{[^}]+\}", "x", path.rstrip(".,;:"))
        assert hermes.allows(method, concrete), f"{skill.parent.name} teaches {method} {path}"
    assert "--env-file secrets.env" not in text
    assert "docker compose" not in text


# --------------------------------------------------------------------------- #
# F6, F7
# --------------------------------------------------------------------------- #

def test_the_client_has_no_stop_verb():
    assert not hasattr(_ops_client().OpsClient, "compose_down")


def test_stack_monitor_reads_the_token_file_when_the_env_is_empty(tmp_path, monkeypatch):
    token_file = tmp_path / "t"
    token_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.delenv("OPS_CONTROLLER_TOKEN", raising=False)
    monkeypatch.setenv("OPS_CONTROLLER_TOKEN_FILE", str(token_file))
    spec = importlib.util.spec_from_file_location("stack_monitor_t", ROOT / "scripts" / "stack_monitor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ops_controller_token() == "from-file"
