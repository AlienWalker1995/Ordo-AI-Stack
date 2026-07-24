# agent-hermes (the default Ordo agent)

V2's Hermes agent image, referenced by the Hermes agent manifest (`agents/hermes/agent.yaml`) via
the `<project>/agent-<id>` convention as `ordo/agent-hermes:latest`, and reused by the
`hermes-dashboard` plugin (V1 runs one Hermes image as both the gateway and the dashboard).

This is an **operator-specific** image: it wraps the operator's Hermes `data/` (SOUL.md, skills,
automation) on top of the pinned Hermes base. V1 builds it from `C:\dev\ordo-ai-stack\hermes`
(`Dockerfile`, multi-stage: node builds the SPA, python runtime installs Hermes at a pinned SHA).
Project buildable image, so `ordo preflight` reports a missing one as "build first".

## Build
```
docker build -t ordo/agent-hermes:latest C:/dev/ordo-ai-stack/hermes
```

Referenced (not duplicated) — the Hermes build context + the operator's `data/` are the single
source of truth. Swapping the agent (see `agents/README.md`) points the `agent` service at a
different image; the core stays agent-agnostic.

Runtime secrets (`OPS_CONTROLLER_TOKEN`, Discord/backup tokens) come from `secrets.env`, never baked.
