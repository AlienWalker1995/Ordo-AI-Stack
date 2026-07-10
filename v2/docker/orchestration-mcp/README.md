# orchestration-mcp (Stable orchestration adapter)

V2's Orchestration MCP image, referenced by the `orchestration` plugin as
`ordo-v2/orchestration-mcp:latest`. V1 builds it locally
(`ordo-ai-stack-orchestration-mcp:latest`) from `C:\dev\ordo-ai-stack\orchestration-mcp` — a thin
adapter exposing STABLE tool names (list_templates / create_from_template / run_workflow /
await_run / list_jobs / publish_enqueue / schedules / registry parity verbs / comfyui ops …) that
delegate over HTTP to the dashboard control plane at `http://dashboard:8080/api/orchestration/*`
(Bearer `DASHBOARD_AUTH_TOKEN`). It insulates the agent from shifting raw gateway tool names.
There is no public registry to digest-pin against, so it's a **project buildable image** (pinned by
its build context); `ordo preflight` reports a missing one as "build first".

Backing verified in V2: the `ordo-v2/dashboard-v1` image serves `/api/orchestration/*`
(readiness probe returns `ok`), so this adapter's target exists in the V2 stack.

## Build
Build from the operator's authoritative source context (kept as the single source of truth — not
duplicated here to avoid drift), tagging the V2 image:
```
docker build -t ordo-v2/orchestration-mcp:latest C:/dev/ordo-ai-stack/orchestration-mcp
```

This image is gateway-spawned (stdio), so it appears in the rendered `mcp-registry.yaml`, not as a
long-lived compose service. It needs network access to reach `dashboard:8080` (`disableNetwork`
unset). `DASHBOARD_AUTH_TOKEN` is substituted from the gateway env into its catalog entry at
gateway startup.
