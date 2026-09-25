"""Agent registry — the multi-agent contract (Hermes is the default; agents are pluggable).

An Ordo "agent" is the orchestrator container that drives the stack. It is swappable: the core
(llama.cpp + gateways + ops-controller + dashboard) is agent-agnostic, and any container that
honours the contract below can be the agent. Like plugins, agents are declared as data manifests,
not code, so a third party ships an agent by dropping a `services/<id>/agent.yaml` in.

The contract every agent image MUST honour (open standards, per the architecture decisions):
  - CHAT: talk to the model via the model-gateway's OpenAI-compatible endpoint (never bind the
    GPU itself) — reads `LLAMACPP_*`-derived config from the rendered `.env`, model id `local-chat`.
  - TOOLS: reach tools through the model-gateway's MCP endpoint (LiteLLM's MCP gateway at /mcp,
    authenticated with the agent's own LiteLLM virtual key), not bespoke integrations.
  - GPU: request heavy GPU work through the ops-controller (`POST /jobs`) and read `GET /status`
    instead of evicting llama.cpp — so the scheduler, not the agent, arbitrates the card.
  - CONFIG: treat the rendered `.env` as read-only truth; never hand-edit derived config.

`image` defaults to the `<project>/agent-<id>` convention (render adds the tag `ordo build` recorded) but a manifest may pin any image.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import yaml

from .buildspec import BuildSpec
from .plugins import parse_derived_env
from .secret_files import SecretFileRef, parse_secret_files

# The core services an agent may declare it consumes — used to validate a manifest isn't asking
# for something the core doesn't provide.
KNOWN_SERVICES = frozenset({"model-gateway", "model-gateway-keys", "ops-controller", "dashboard"})


@dataclasses.dataclass(frozen=True)
class Agent:
    id: str
    name: str
    description: str
    image: str                       # "" -> resolved to the <project>/agent-<id> convention
    default: bool
    consumes: tuple[str, ...]
    env: dict[str, str]
    command: tuple[str, ...]         # () -> compose omits it and the image's default CMD runs
    # ── runtime wiring (data-driven parity with the V1 agent container; phase-5.5 audit) ──
    user: str = ""                   # "" -> compose omits `user:` and the image default applies
    # Supplementary groups (compose `group_add`). Hermes needs `["0"]` (root group) so the
    # unprivileged `hermes` user can reach the root:root docker.sock (mode 660) it mounts — the
    # same pattern the control plane uses. Empty -> compose omits it.
    group_add: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()    # bind/volume specs (src:dst[:ro]); ${VAR} refs pass through
    environment: dict[str, str] = dataclasses.field(default_factory=dict)  # non-secret env
    # Secrets the agent reads from a file under /run/secrets (ordo/secret_files.py), the same
    # declaration every service manifest uses.
    secret_files: tuple[SecretFileRef, ...] = ()
    # Env-var secret NAMES the agent reads, rendered as `KEY: ${KEY}` (see PluginService.secrets).
    secrets: tuple[str, ...] = ()
    # Derived-config NAMES (keys of the rendered .env) the agent reads, rendered as
    # `KEY: ${KEY?...}` (see PluginService.derived_env). The agent never loads the whole .env.
    derived_env: tuple[str, ...] = ()
    # depends_on with optional health conditions: {peer: "service_healthy"|"service_started"}.
    # Empty -> compose omits it (render adds the core-peer list). A value -> emitted with conditions.
    depends_on: dict[str, str] = dataclasses.field(default_factory=dict)
    healthcheck: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Build-context identity (METADATA for preflight/tests; NEVER rendered into compose). Agents are
    # pluggable: an operator/third-party often ships a PREBUILT image (no in-repo Dockerfile) — those
    # declare `build: {external: true}`. Absent -> the agent's own `services/<id>/`. See ordo.buildspec.
    build: BuildSpec = dataclasses.field(default_factory=BuildSpec)
    # Optional per-consumer LiteLLM virtual key: {models: [group,...], mcp_servers: all|[server_id,...]}.
    # render derives the env var LITELLM_KEY_<ID>, adds it to required secrets, and emits the grant
    # into out/model-gateway/keys.json for bootstrap_keys.py. Empty -> this agent gets no key.
    litellm_key: dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Agent:
        secrets = tuple(str(k) for k in (d.get("secrets", []) or []))
        return cls(
            id=str(d["id"]), name=str(d.get("name", d["id"])),
            description=str(d.get("description", "")),
            image=str(d.get("image", "")),
            default=bool(d.get("default", False)),
            consumes=tuple(d.get("consumes", []) or []),
            env={str(k): str(v) for k, v in (d.get("env", {}) or {}).items()},
            command=tuple(str(c) for c in (d.get("command", []) or [])),
            user=str(d.get("user", "") or ""),
            group_add=tuple(str(g) for g in (d.get("group_add", []) or [])),
            volumes=tuple(str(v) for v in (d.get("volumes", []) or [])),
            environment={str(k): str(v) for k, v in (d.get("environment", {}) or {}).items()},
            secret_files=parse_secret_files(f"agent {d.get('id')!r}", d.get("secret_files"), env_secrets=secrets,
                                            explicit_env=d.get("environment") or {}),
            depends_on={str(k): str(v) for k, v in (d.get("depends_on", {}) or {}).items()},
            healthcheck=dict(d.get("healthcheck", {}) or {}),
            build=BuildSpec.from_dict(d.get("build")),
            litellm_key=dict(d.get("litellm_key", {}) or {}),
            secrets=secrets,
            derived_env=parse_derived_env(f"agent {d.get('id')!r}", d),
        )

    def image_for(self, project: str) -> str:
        return self.image or f"{project}/agent-{self.id}"

    def unknown_services(self) -> list[str]:
        return [s for s in self.consumes if s not in KNOWN_SERVICES]


class AgentRegistry:
    def __init__(self, agents: list[Agent]):
        self.agents = agents
        self._by_id = {a.id: a for a in agents}

    @classmethod
    def load(cls, agents_dir: str | Path) -> AgentRegistry:
        # Co-located manifests: each agent declares itself in `services/<id>/agent.yaml`.
        # sorted() over the glob keys the registry by path (== by folder id) so order is stable.
        base = Path(agents_dir)
        agents = [
            Agent.from_dict(yaml.safe_load(manifest.read_text(encoding="utf-8")) or {})
            for manifest in sorted(base.glob("*/agent.yaml"))
        ]
        return cls(agents)

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
