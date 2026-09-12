# comfyui-mcp (ComfyUI MCP server)

Build context for the ComfyUI MCP image, referenced by the `comfyui-mcp` plugin
([`plugin.yaml`](plugin.yaml)) as `ordo/comfyui-mcp:latest`. It bundles the upstream
`joenorton/comfyui-mcp-server` (pinned to commit `e0101b2312f3`) plus the stack's overrides
(stdio-clean `print()` redirect, flat `run_workflow` args + default workflow, custom-node pip +
comfyui-restart tools via ops-controller, and system-state tools: GPU / queue / models / nodes /
extensions). It talks to `http://comfyui:8188` and `http://ops-controller:9000`. There is no public
registry to digest-pin against, so it's a **project buildable image** (pinned by its build context);
`ordo preflight` reports a missing one as "build first".

## Build
```
docker build -t ordo/comfyui-mcp:latest services/comfyui-mcp
```

This directory (`Dockerfile`, `managers/`, `tools/`) is the single source of truth for the service.

This image runs as the long-lived compose service `mcp-comfyui` (rendered into
`out/model-gateway/mcp_servers.yaml` and `out/mcp/servers.json`), serving streamable HTTP on
port 9000. Its manifest sets `network: stack` because it must reach `comfyui:8188` and
`ops-controller` for the GPU lease. `COMFYUI_URL`, `COMFY_MCP_DEFAULT_MODEL` and
`OPS_CONTROLLER_TOKEN` come from the manifest's `env:` block, compose-interpolated from the
rendered `.env` and `secrets.env`.
