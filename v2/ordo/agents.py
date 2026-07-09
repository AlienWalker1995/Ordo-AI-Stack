"""Agent registry — the multi-agent contract (Hermes is the default; agents are pluggable).

An Ordo "agent" is the orchestrator container that drives the stack. It is swappable: the core
(llama.cpp + gateways + ops-controller + dashboard) is agent-agnostic, and any container that
honours the contract below can be the agent. Like plugins, agents are declared as data manifests,
not code, so a third party ships an agent by dropping an `agents/<id>/agent.yaml` in.

The contract every agent image MUST honour (open standards, per the architecture decisions):
  - CHAT: talk to the model via the model-gateway's OpenAI-compatible endpoint (never bind the
    GPU itself) — reads `LLAMACPP_*`-derived config from the rendered `.env`, model id `local-chat`.
  - TOOLS: reach tools through the mcp-gateway (MCP), not bespoke integrations.
  - GPU: request heavy GPU work through the ops-controller (`POST /jobs`) and read `GET /status`
    instead of evicting llama.cpp — so the scheduler, not the agent, arbitrates the card.
  - CONFIG: treat the rendered `.env` as read-only truth; never hand-edit derived config.

`image` defaults to the `<project>/agent-<id>:latest` convention but a manifest may pin any image.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

# The core services an agent may declare it consumes — used to validate a manifest isn't asking
# for something the core doesn't provide.
KNOWN_SERVICES = frozenset({"model-gateway", "mcp-gateway", "ops-controller", "dashboard"})


@dataclasses.dataclass(frozen=True)
class Agent:
    id: str
    name: str
    description: str
    image: str                       # "" -> resolved to the <project>/agent-<id>:latest convention
    default: bool
    consumes: tuple[str, ...]
    env: dict[str, str]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Agent":
        return cls(
            id=str(d["id"]), name=str(d.get("name", d["id"])),
            description=str(d.get("description", "")),
            image=str(d.get("image", "")),
            default=bool(d.get("default", False)),
            consumes=tuple(d.get("consumes", []) or []),
            env={str(k): str(v) for k, v in (d.get("env", {}) or {}).items()},
        )

    def image_for(self, project: str) -> str:
        return self.image or f"{project}/agent-{self.id}:latest"

    def unknown_services(self) -> list[str]:
        return [s for s in self.consumes if s not in KNOWN_SERVICES]


class AgentRegistry:
    def __init__(self, agents: list[Agent]):
        self.agents = agents
        self._by_id = {a.id: a for a in agents}

    @classmethod
    def load(cls, agents_dir: str | Path) -> "AgentRegistry":
        base = Path(agents_dir)
        found: list[Agent] = []
        if base.is_dir():
            for manifest in sorted(base.glob("*/agent.yaml")):
                data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
                found.append(Agent.from_dict(data))
        return cls(found)

    def get(self, agent_id: str) -> Agent | None:
        return self._by_id.get(agent_id)

    def default_agent(self) -> Agent | None:
        for a in self.agents:
            if a.default:
                return a
        return self.agents[0] if self.agents else None

    def resolve(self, agent_id: str) -> tuple[Agent | None, list[str]]:
        """Resolve the chosen agent. Unknown id -> a note (caller falls back to the convention),
        so a typo is surfaced loudly at render/preflight rather than as a mystery at compose-up."""
        notes: list[str] = []
        a = self._by_id.get(agent_id)
        if a is None:
            avail = ", ".join(sorted(self._by_id)) or "(none registered)"
            notes.append(f"agent '{agent_id}' is not in the registry (available: {avail})")
            return None, notes
        notes += [f"agent '{a.id}': declares unknown service '{s}'" for s in a.unknown_services()]
        return a, notes
