# DockerOps — Subagent Protocol

**When to use:** User asks to start/stop/restart services, pull models, check service status, view logs, or manage the stack.

**Activate by reading this file, then follow the protocol below.**

**Critical constraint:** You run inside a container. `docker` and `docker compose` CLI are NOT available via exec. All Docker operations go through the Dashboard API or Ops Controller API.

---

## Service operations

All via Dashboard API (requires `DASHBOARD_AUTH_TOKEN`):

```bash
# Start a service
wget -q -O - --post-data='' \
  --header='Authorization: Bearer '"$DASHBOARD_AUTH_TOKEN" \
  "$DASHBOARD_URL/api/ops/services/{name}/start"

# Stop a service
wget -q -O - --post-data='' \
  --header='Authorization: Bearer '"$DASHBOARD_AUTH_TOKEN" \
  "$DASHBOARD_URL/api/ops/services/{name}/stop"

# Restart a service
wget -q -O - --post-data='' \
  --header='Authorization: Bearer '"$DASHBOARD_AUTH_TOKEN" \
  "$DASHBOARD_URL/api/ops/services/{name}/restart"

# View logs (last 100 lines)
wget -q -O - \
  --header='Authorization: Bearer '"$DASHBOARD_AUTH_TOKEN" \
  "$DASHBOARD_URL/api/ops/services/{name}/logs?lines=100"
```

**Allowed service names:** `ollama`, `dashboard`, `open-webui`, `model-gateway`, `mcp-gateway`, `comfyui`, `n8n`, `openclaw-gateway`, `qdrant`

## Health check

```bash
wget -q -O - --header='Authorization: Bearer '"$DASHBOARD_AUTH_TOKEN" \
  "$DASHBOARD_URL/api/health"
```

Always run health check **before** and **after** any service operation. If the service is already healthy, do not restart it.

## Model downloads (ComfyUI)

Start download:
```bash
wget -q -O - \
  --post-data='{"url":"https://...","category":"checkpoints","filename":"model.safetensors"}' \
  --header='Content-Type: application/json' \
  --header='Authorization: Bearer '"$DASHBOARD_AUTH_TOKEN" \
  "$DASHBOARD_URL/api/models/download"
```

Poll status (repeat every 30s until `"done": true`):
```bash
wget -q -O - --header='Authorization: Bearer '"$DASHBOARD_AUTH_TOKEN" \
  "$DASHBOARD_URL/api/models/download/status"
```

## Ollama model management

```bash
# List models
wget -q -O - --header='Authorization: Bearer '"$DASHBOARD_AUTH_TOKEN" \
  "$DASHBOARD_URL/api/ollama/models"

# Pull a model (streaming — shows progress)
wget -q -O - \
  --post-data='{"name":"deepseek-r1:7b"}' \
  --header='Content-Type: application/json' \
  --header='Authorization: Bearer '"$DASHBOARD_AUTH_TOKEN" \
  "$DASHBOARD_URL/api/ollama/pull"
```

## Hardware / resource check

```bash
wget -q -O - "$DASHBOARD_URL/api/hardware"
```

Returns CPU%, RAM, disk, and GPU stats (no auth required).

---

## Rules

- **Always check health first** — never restart a healthy service without cause
- **Always confirm destructive operations** — stopping a service, clearing queues, deleting files
- **Never restart `dashboard` or `ops-controller`** unless the user explicitly asks — losing the management plane is disruptive
- Report the result of every operation (success/failure, new service state)
