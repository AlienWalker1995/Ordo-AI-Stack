# orchestration-mcp (Stable orchestration adapter)

Build context for the Orchestration MCP image, referenced by the `orchestration` plugin
([`plugin.yaml`](plugin.yaml)) as `ordo/orchestration-mcp:latest`. A thin adapter exposing STABLE
tool names (list_templates / create_from_template / run_workflow / await_run / list_jobs /
publish_enqueue / schedules / registry parity verbs / comfyui ops …) that delegate over HTTP to the
dashboard control plane at `http://dashboard:8080/api/orchestration/*` (no Bearer token — reached
over the internal `ordo-net` network; the Caddy edge's oauth2-proxy SSO is the auth gate for the
dashboard, not a per-service token). It insulates the agent from shifting raw gateway tool names.
There is no public registry to digest-pin against, so it's a **project buildable image** (pinned by
its build context); `ordo preflight` reports a missing one as "build first".

Backing verified: the `ordo/dashboard-v1` image serves `/api/orchestration/*` (readiness probe
returns `ok`), so this adapter's target exists in the stack.

## Build
```
docker build -t ordo/orchestration-mcp:latest services/orchestration
```

This directory (`server.py`, `requirements.txt`, `Dockerfile`) is the single source of truth for
the service.

This image is gateway-spawned (stdio), so it appears in the rendered `mcp-registry.yaml`, not as a
long-lived compose service. It needs network access to reach `dashboard:8080` (`disableNetwork`
unset). No auth token is substituted into its catalog entry — the dashboard has no per-service auth
token in the Ordo deployment, so calls over `ordo-net` go unauthenticated by design (the Caddy edge
is the sole authentication gate, and it isn't in this internal service-to-service path).
