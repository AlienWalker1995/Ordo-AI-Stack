# agent-hermes (the default Ordo agent)

V2's Hermes agent image, referenced by the Hermes agent manifest (`services/hermes/agent.yaml`) via
the `<project>/agent-<id>` convention as `ordo/agent-hermes`, and reused by the
`hermes-dashboard` plugin (one Hermes image serves as both the gateway and the dashboard).

It is upstream's published `nousresearch/hermes-agent` image (pinned by digest) plus this stack's
layer: a boot hook (`ordo-init.sh`, run by upstream's s6 boot) that configures Hermes through its own
CLI, the `ops-router` plugin, the ComfyUI skill notes and the seed `SOUL.md`. Hermes itself is never
modified. It builds from this co-located context `services/hermes/`; project buildable image, so
`ordo preflight` reports a missing one as "build first".

## Build
```
ordo build agent
```

Referenced (not duplicated) — the Hermes build context + the operator's `data/` are the single
source of truth. Swapping the agent (see `../../docs/agents.md`) points the `agent` service at a
different image; the core stays agent-agnostic.

Runtime secrets (`OPS_CONTROLLER_TOKEN`, Discord/backup tokens) come from `secrets.env`, never baked.
