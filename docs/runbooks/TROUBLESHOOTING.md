# Troubleshooting Runbook

## Quick Diagnostics

```bash
# Service status
docker compose ps

# Recent logs
docker compose logs --tail=50

# Health checks
curl -s http://localhost:8080/api/health | jq
curl -s http://localhost:11435/health | jq
curl -s http://localhost:8811/mcp
```

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
- **OpenClaw**: See [openclaw/README.md](../../openclaw/README.md)
