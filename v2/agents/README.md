# Agents — the pluggable orchestrator layer

The Ordo core (llama.cpp + model-gateway + mcp-gateway + ops-controller + dashboard) is
**agent-agnostic**. The *agent* is the orchestrator container that drives it, and it's swappable —
**Hermes is the default**, but any container honouring the contract can take its place.

## The contract

An Ordo agent image MUST:

1. **Chat** via the model-gateway's **OpenAI-compatible** endpoint (model id `local-chat`). It never
   binds the GPU directly — reads the rendered `.env` for connection config.
2. **Use tools** through the **mcp-gateway** (MCP), not bespoke integrations.
3. **Request GPU work** through the ops-controller: `POST /jobs` to reserve VRAM and `GET /status`
   to see scheduler state — so the *scheduler* arbitrates the card, not the agent. (This is what
   ends the eviction deadlock: an agent can't evict llama.cpp by starting a render; it asks, and
   the scheduler co-runs or queues it.)
4. **Treat the rendered `.env` as read-only truth** — never hand-edit derived config (drift cure).

## Adding an agent

Drop a manifest at `agents/<id>/agent.yaml`:

```yaml
id: my-agent
name: My Agent
description: what it is
default: false                 # exactly one agent should be default: true
image: ghcr.io/me/my-agent:latest   # omit -> <project>/agent-<id>:latest (operator builds it)
consumes:                      # validated against the core services
  - model-gateway
  - mcp-gateway
  - ops-controller
```

Then select it in `ordo.yaml`:

```yaml
agent: my-agent
```

`ordo render` resolves the agent from the registry and wires its image into the compose `agent`
service. An unknown id is surfaced as a warning at render/preflight (and falls back to the naming
convention) rather than failing mysteriously at `compose up`.

See [`hermes/agent.yaml`](hermes/agent.yaml) (the default, operator-built) and
[`openai-agent/agent.yaml`](openai-agent/agent.yaml) (a pinned generic reference adapter).
