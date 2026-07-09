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
    assert hermes.image_for("ordo-v2") == "ordo-v2/agent-hermes:latest"


def test_pinned_image_is_honored():
    a = AGENTS.get("openai-agent")
    assert a.image_for("ordo-v2") == "ghcr.io/ordo-ai/agent-openai-compat:latest"


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
    assert c["services"]["agent"]["image"] == "ordo-v2/agent-hermes:latest"


def test_render_swaps_to_pinned_agent_image(tmp_path):
    render(_src("openai-agent"), CATALOG, REGISTRY, agents=AGENTS).write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert c["services"]["agent"]["image"] == "ghcr.io/ordo-ai/agent-openai-compat:latest"


def test_render_unknown_agent_warns_and_falls_back(tmp_path):
    rc = render(_src("typo-agent"), CATALOG, REGISTRY, agents=AGENTS)
    assert any("typo-agent" in w and "registry" in w for w in rc.warnings)
    rc.write(tmp_path)
    c = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    assert c["services"]["agent"]["image"] == "ordo-v2/agent-typo-agent:latest"  # convention fallback
