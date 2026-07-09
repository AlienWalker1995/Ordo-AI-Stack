# mcp-gateway (docker/mcp-gateway config-wrapper)

V2's `mcp-gateway` core service. This is the small wrapper build V1 runs
(`ordo-ai-stack-mcp-gateway:latest`) — the pinned `docker/mcp-gateway` base plus a runtime
config-reload wrapper (`gateway-wrapper.sh`) and a catalog-aware healthcheck.

V2 references it as a **project buildable image** (`ordo-v2/mcp-gateway:latest`) — pinned by its
build context, not pulled — so `ordo preflight` reports a missing one as "build first". V2 does
NOT reference the bare upstream `docker/mcp-gateway:latest` because it lacks the reload wrapper +
banner-suppression handling the stack depends on.

The MCP server catalog is NOT baked into this image: the gateway reads the **rendered**
`mcp-registry.yaml` (which `ordo render` regenerates from the enabled `kind=mcp` plugins) from
its mounted config dir at runtime — one source of truth, no drift.

## Build
```
docker build -t ordo-v2/mcp-gateway:latest v2/docker/mcp-gateway
```

## Files
- `Dockerfile` — pins `docker/mcp-gateway:v2`, adds jq/curl + the wrapper entrypoint.
- `gateway/gateway-wrapper.sh` — runtime catalog substitution + reload.
- `gateway/healthcheck.sh` — verifies the gateway loaded a non-empty tool catalog.

GitHub / n8n API tokens for spawned MCP servers are supplied at runtime from `secrets.env`.
