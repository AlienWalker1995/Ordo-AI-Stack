# comfyui-mcp (ComfyUI MCP server)

V2's ComfyUI MCP image, referenced by the `comfyui` plugin as `ordo-v2/comfyui-mcp:latest`.
V1 builds it locally (`ordo-ai-stack-comfyui-mcp:latest`) from `C:\dev\ordo-ai-stack\comfyui-mcp`
— the upstream `joenorton/comfyui-mcp-server` (pinned to commit `e0101b2312f3`) plus the stack's
overrides (stdio-clean `print()` redirect, flat `run_workflow` args + default workflow, custom-node
pip + comfyui-restart tools via ops-controller, and system-state tools: GPU / queue / models /
nodes / extensions). It talks to `http://comfyui:8188` and `http://ops-controller:9000`. There is
no public registry to digest-pin against, so it's a **project buildable image** (pinned by its
build context); `ordo preflight` reports a missing one as "build first".

## Build
Build from the operator's authoritative source context (kept as the single source of truth — not
duplicated here to avoid drift), tagging the V2 image:
```
docker build -t ordo-v2/comfyui-mcp:latest C:/dev/ordo-ai-stack/comfyui-mcp
```

This image is gateway-spawned (stdio), so it appears in the rendered `mcp-registry.yaml`, not as a
long-lived compose service. It needs network access to reach `comfyui:8188` (`disableNetwork`
unset). `COMFY_MCP_DEFAULT_MODEL` + `OPS_CONTROLLER_TOKEN` are substituted from the gateway env
into its catalog entry at gateway startup.
