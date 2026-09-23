# orchestration-mcp (Stable orchestration adapter)

Build context for the Orchestration MCP image, referenced by the `orchestration` plugin
([`plugin.yaml`](plugin.yaml)) as `ordo/orchestration-mcp:latest`. A thin adapter exposing STABLE
tool names (list_templates / create_from_template / validate, save, diff, promote and rollback
workflows / list_outputs / comfyui status and restart / model registry and GPU status /
list_model_catalog / set_active_model) that delegate over HTTP to the dashboard at
`http://dashboard:8080/api/orchestration/*` and `/api/models*` (no Bearer token, reached
over the internal `ordo-net` network; the Caddy edge's oauth2-proxy SSO is the auth gate for the
dashboard, not a per-service token). It insulates the agent from shifting raw gateway tool names.
There is no public registry to digest-pin against, so it's a **project buildable image** (pinned by
its build context); `ordo preflight` reports a missing one as "build first".

Backing: the `ordo/dashboard` image serves `/api/orchestration/*`
(`services/dashboard/dashboard/routes_orchestration.py`) and `/api/models*`
(`routes_console.py`), so this adapter's target exists in the stack.

## Build
```
docker build -t ordo/orchestration-mcp:latest services/orchestration
```

This directory (`server.py`, `requirements.txt`, `Dockerfile`) is the single source of truth for
the service.

This image runs as the long-lived compose service `mcp-orchestration` (rendered into
`out/model-gateway/mcp_servers.yaml` and `out/mcp/servers.json`), serving streamable HTTP on
port 9000. Its manifest sets `network: stack` because it must reach `dashboard:8080`.
No auth token is set in its `env:` block — the dashboard has no per-service auth
token in the Ordo deployment, so calls over `ordo-net` go unauthenticated by design (the Caddy edge
is the sole authentication gate, and it isn't in this internal service-to-service path).
