# stack-ops.md

Stack lifecycle operations runbook. Read this when asked to check service health, restart a service, pull a model, read logs, or perform any ops-controller action.

## Service Hostnames

| Service | Base URL | Auth |
|---------|---------|------|
| ops-controller | `http://ops-controller:9000` | `Authorization: Bearer {{ secret.OPS_CONTROLLER_TOKEN }}` |
| dashboard | `http://dashboard:8080` | `Authorization: Bearer {{ secret.DASHBOARD_AUTH_TOKEN }}` |
| model-gateway | `http://model-gateway:11435/v1` | Header: `X-Service-Name: openclaw` |
| n8n | `http://n8n:5678` | via MCP only (`gateway__n8n__*`) |

Never use `localhost` — always use service hostnames inside the stack.

## Blast-Radius Decision Tree

Before any Tier 1+ action, walk this tree:

```
1. Is the service in ALLOWED_SERVICES?
   No → Stop. Tell the user. Do not proceed.

2. Is this a read-only action (Tier 0)?
   Yes → Execute immediately.

3. Is this a comfyui restart (Tier 1)?
   → GET /services (check comfyui state)
   → If any job shows state=running or queue is non-empty → abort, report
   → Otherwise → POST /services/comfyui/restart
   → Poll GET /services until comfyui.status == "running"
   → Report result

4. Is this a non-comfyui restart, stop, or env/set (Tier 2)?
   → State intent to user: "I'm going to [action] [service] because [reason]"
   → Wait for user to acknowledge in this turn
   → Execute, then verify via GET /services

5. Is this a Tier 3 action?
   → Stop immediately. Explain the constraint. Ask the user to perform the action on the host.
```

## ComfyUI Self-Healing Flow

1. Detect: `GET /services` → `comfyui.status != "running"` or MCP tool calls returning errors
2. Verify no active generation: check MCP queue status (`gateway__comfyui__get_queue_status` or `GET /services`)
3. If queue is clear → `POST /services/comfyui/restart` (Tier 1, no confirmation needed)
4. Poll `GET /services` every 5s until `comfyui.status == "running"` (timeout: 60s)
5. Verify: call `gateway__comfyui__list_workflows` to confirm MCP tools respond
6. Report outcome

## GGUF Model Pull Workflow

1. Confirm model repo id with user (e.g. `bartowski/Qwen3.5-14B-Instruct-GGUF`)
2. `POST /models/gguf-pull` with body `{"repos": "<repo-id>", "quantizations": ["Q4_K_M"]}` + Bearer auth
3. Poll `GET /models/gguf-pull/status` until `done == true`
4. Check `success` field; report `output` on failure
5. Restart llamacpp only after user confirms (Tier 2: "Restart llamacpp to load the new model?")

## Audit Log Correlation

- Include `X-Request-ID: <short-id>` header on all POST calls to ops-controller
- Read back: `GET /audit?limit=20` — find your request by timestamp or correlation
- Note: `actor` field in audit entries reads `"dashboard"` for proxied calls — this is expected; OpenClaw's identity is established by the Bearer token, not the actor label

## n8n Workflow Operations

- **List:** `gateway__n8n__list_workflows` or `gateway__call` with `tool: "n8n__list_workflows"`
- **Execute:** `gateway__n8n__execute_workflow` with workflow id and input data
- **Never create or modify** n8n workflows from within OpenClaw — n8n is the durable automation publisher, not OpenClaw
- `N8N_API_KEY` is consumed by `mcp-gateway`, not openclaw-gateway; no token needed in tool args

## Cross-References

- Full endpoint table: `TOOLS.md` → Stack Lifecycle (ops-controller)
- Autonomy tiers: `AGENTS.md` → Stack Autonomy Tiers
- ComfyUI model pack pulls (MCP path): `agents/docker-ops.md`
- Service URL table: `TOOLS.md` → Core services
