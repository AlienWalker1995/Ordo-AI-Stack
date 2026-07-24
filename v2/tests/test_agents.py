"""Agents are pluggable data manifests; Hermes is the default; render resolves the image."""
from pathlib import Path

import yaml

from ordo.agents import AgentRegistry
from ordo.catalog import Catalog
from ordo.config import Source
from ordo.plugins import PluginRegistry
from ordo.render import render

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Catalog.load(ROOT / "catalog" / "models.yaml")
REGISTRY = PluginRegistry.load(ROOT / "plugins")
AGENTS = AgentRegistry.load(ROOT / "agents")


def test_hermes_is_the_default():
    d = AGENTS.default_agent()
    assert d is not None and d.id == "hermes" and d.default


def test_image_convention_when_unpinned():
    hermes = AGENTS.get("hermes")
    assert hermes.image == ""                                   # unpinned -> operator builds it
    assert hermes.image_for("ordo") == "ordo/agent-hermes:latest"


def test_pinned_image_is_honored():
    a = AGENTS.get("openai-agent")
    assert a.image_for("ordo") == "ghcr.io/ordo-ai/agent-openai-compat:latest"


def test_unknown_agent_is_flagged_not_crashed():
    a, notes = AGENTS.resolve("nope")
    assert a is None and any("not in the registry" in n for n in notes)


def test_unknown_declared_service_flagged():
    from ordo.agents import Agent
    bad = Agent.from_dict({"id": "x", "consumes": ["model-gateway", "quantum-gateway"]})
    assert bad.unknown_services() == ["quantum-gateway"]


def _src(agent):
    return Source.from_dict({"hardware": {"gpus": [{"vram_gb": 32}], "ram_gb": 128},
                             "model": "auto", "plugins": "auto", "agent": agent})


def test_render_default_agent_uses_convention_image(tmp_path):
    render(_src("hermes"), CATALOG, REGISTRY, agents=AGENTS).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert c["services"]["agent"]["image"] == "ordo/agent-hermes:latest"


def test_hermes_manifest_declares_gateway_command():
    # the agent-hermes image's default CMD is `hermes --help` (prints usage + exits); the manifest
    # must start the persistent gateway or the container restart-loops. Regression guard for the
    # phase-5 flip attempt #2 rollback.
    assert AGENTS.get("hermes").command == ("hermes", "gateway")


def test_render_agent_emits_gateway_command(tmp_path):
    # the rendered agent service MUST override the image's no-op default CMD with `hermes gateway`,
    # mirroring V1's compose. Without this the agent boots into `hermes --help` and crash-loops.
    render(_src("hermes"), CATALOG, REGISTRY, agents=AGENTS).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert c["services"]["agent"]["command"] == ["hermes", "gateway"]


def test_render_agent_without_command_omits_it(tmp_path):
    # an agent whose image self-starts (no manifest `command`) leaves compose `command` unset so the
    # image default runs — the openai-agent manifest declares none.
    render(_src("openai-agent"), CATALOG, REGISTRY, agents=AGENTS).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert "command" not in c["services"]["agent"]


def test_render_swaps_to_pinned_agent_image(tmp_path):
    render(_src("openai-agent"), CATALOG, REGISTRY, agents=AGENTS).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert c["services"]["agent"]["image"] == "ghcr.io/ordo-ai/agent-openai-compat:latest"


def test_render_unknown_agent_warns_and_falls_back(tmp_path):
    rc = render(_src("typo-agent"), CATALOG, REGISTRY, agents=AGENTS)
    assert any("typo-agent" in w and "registry" in w for w in rc.warnings)
    rc.write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert c["services"]["agent"]["image"] == "ordo/agent-typo-agent:latest"  # convention fallback


# ── Defect class: agent runtime wiring (V2 agent had NO volumes/secrets/env/healthcheck → the brain,
#    /workspace data, the /c/dev mirror + file secrets were all missing; only mattered live). ──
def test_hermes_manifest_declares_runtime_wiring():
    h = AGENTS.get("hermes")
    assert h.user == "root"
    assert h.secret_files and all("target" in s and "source" in s for s in h.secret_files)
    assert h.depends_on.get("model-gateway") == "service_healthy"
    assert h.healthcheck  # gateway_state.json check


def test_render_agent_mounts_brain_workspace_and_mirror(tmp_path):
    render(_src("hermes"), CATALOG, REGISTRY, agents=AGENTS).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    vols = c["services"]["agent"]["volumes"]
    # THE BRAIN at the staged path (never the live-stack path), plus /workspace/data + /c/dev mirror
    assert any(v.endswith(":/home/hermes/.hermes") and "DATA_PATH" in v for v in vols)
    assert any(v.endswith(":/workspace/data") for v in vols)
    assert any(v.endswith(":/c/dev") for v in vols)


def test_render_agent_mounts_file_secrets_readonly(tmp_path):
    render(_src("hermes"), CATALOG, REGISTRY, agents=AGENTS).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    vols = c["services"]["agent"]["volumes"]
    assert any(v.endswith(":/run/secrets/discord_token:ro") for v in vols)
    assert any(v.endswith(":/run/secrets/github_backup_pat:ro") for v in vols)


def test_render_agent_has_user_env_and_healthcheck(tmp_path):
    render(_src("hermes"), CATALOG, REGISTRY, agents=AGENTS).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    agent = c["services"]["agent"]
    assert agent["user"] == "root"
    assert agent["environment"]["OPS_CONTROLLER_URL"] == "http://ops-controller:9000"
    assert agent["healthcheck"]["test"][-1].endswith("gateway_state.json")


def test_service_healthy_depends_targets_all_have_healthchecks(tmp_path):
    # Live-only failure class: the agent gates on `dashboard: service_healthy` (audit G5), but if
    # the dashboard service renders WITHOUT a healthcheck, that gate is permanently unsatisfiable
    # and Compose refuses to start the agent ("has no healthcheck configured"). Assert every
    # service the agent depends on via `service_healthy` actually renders a healthcheck.
    render(_src("hermes"), CATALOG, REGISTRY, agents=AGENTS).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    svcs = c["services"]
    dep = svcs["agent"].get("depends_on", {})
    healthy_gated = [p for p, cond in dep.items()
                     if (cond.get("condition") if isinstance(cond, dict) else cond) == "service_healthy"]
    assert "dashboard" in healthy_gated  # guard the exact defect that rolled back attempt #3
    for peer in healthy_gated:
        assert "healthcheck" in svcs[peer], f"{peer} is service_healthy-gated but has no healthcheck"


def test_render_agent_without_wiring_stays_minimal(tmp_path):
    # a third-party agent that declares no runtime wiring renders exactly as before (no volumes etc.)
    render(_src("openai-agent"), CATALOG, REGISTRY, agents=AGENTS).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    agent = c["services"]["agent"]
    assert "volumes" not in agent and "user" not in agent
