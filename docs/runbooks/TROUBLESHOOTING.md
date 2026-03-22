# Troubleshooting Runbook

## Quick Diagnostics

```bash
# Service status
docker compose ps

# Recent logs
docker compose logs --tail=50

# Health checks (host → published ports)
curl -s http://localhost:8080/api/health | jq
curl -s http://localhost:11435/health | jq
curl -s http://localhost:8811/mcp

# Full dependency matrix (same data as Dashboard → Dependencies)
curl -s http://localhost:8080/api/dependencies | jq

# Model Gateway: L2 readiness — HTTP 200 when models are listed; 503 if backends are down or no models
curl -s http://localhost:11435/ready | jq
# Optional: print only status code
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:11435/ready
```

**One-shot host probe + OpenClaw config check** (stack must be up for HTTP checks):

```bash
./scripts/doctor.sh
```

```powershell
.\scripts\doctor.ps1
```

**OpenClaw `openclaw.json`** (typical path: `data/openclaw/openclaw.json`):

```bash
python scripts/validate_openclaw_config.py data/openclaw/openclaw.json
```

The Dashboard **Dependencies** section runs probes **from inside the dashboard container** (Docker DNS names). Optional services (for example Qdrant without the `rag` profile, or ComfyUI if not started) may correctly show as unreachable until those profiles or services are enabled.

**`doctor` script (host):** Uses a long timeout for `GET /api/dependencies` and sends `DASHBOARD_AUTH_TOKEN` from the environment or repo `.env` when probing the dashboard. **WARN** on **HTTP 404** for `/api/dependencies` or `/ready` usually means the **running Docker image is older than the repo** — rebuild `dashboard` and/or `model-gateway` (see README). **FAIL** on Ollama or MCP usually means those services are not listening on localhost (e.g. compose not running or different ports).

## If services fail

Check logs for the failing service:

```bash
docker compose logs <service-name>
```

| Service        | Logs                    |
|----------------|-------------------------|
| Dashboard      | `docker compose logs dashboard` |
| Model Gateway  | `docker compose logs model-gateway` |
| MCP Gateway    | `docker compose logs mcp-gateway` |
| Ops Controller | `docker compose logs ops-controller` |

## Escalation

- **Security**: See [SECURITY.md](../../SECURITY.md)
- **Architecture**: See [Product Requirements Document](../Product%20Requirements%20Document.md)
- **OpenClaw**: Web Control UI defaults to gateway port **6680** (`http://localhost:6680/?token=...`). **6682** is the browser/CDP bridge only. See [openclaw/README.md](../../openclaw/README.md).
