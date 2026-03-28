# Component: Ops Controller

## Purpose

Secure, authenticated REST API for Docker Compose lifecycle operations. The controller holds `docker.sock` so that the dashboard and other clients never need direct Docker access.

## API Reference

**Base URL:** `http://ops-controller:9000` (internal network; no host port)

**Auth:** `Authorization: Bearer <OPS_CONTROLLER_TOKEN>`

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/health` | GET | None | Liveness |
| `/services` | GET | None | List compose services + state |
| `/services/{id}/start` | POST | Bearer | Start (confirm: true required) |
| `/services/{id}/stop` | POST | Bearer | Stop (confirm: true required) |
| `/services/{id}/restart` | POST | Bearer | Restart (confirm: true required) |
| `/services/{id}/logs` | GET | Bearer | Tail logs (tail=100 max 500) |
| `/images/pull` | POST | Bearer | Pull images for services |
| `/mcp/containers` | GET | Bearer | List MCP server containers |
| `/audit` | GET | Bearer | Audit log (limit=50) |

**Safety:** All mutating endpoints require `{"confirm": true}`. Optional `{"dry_run": true}` returns planned action without executing.

## Audit Event Pipeline

### Schema

```json
{
  "ts": "2026-03-01T12:34:56.789Z",
  "action": "restart",
  "resource": "ollama",
  "actor": "dashboard",
  "result": "ok",
  "detail": "",
  "correlation_id": "req-abc123",
  "metadata": {"dry_run": false}
}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ts` | string | Yes | ISO8601 UTC |
| `action` | enum | Yes | `start\|stop\|restart\|pull\|logs\|mcp_add\|mcp_remove\|model_pull\|model_delete` |
| `resource` | string | Yes | Service ID, model name, or tool name |
| `actor` | string | Yes | `dashboard\|cli\|api` |
| `result` | enum | Yes | `ok\|error` |
| `detail` | string | No | Error message or context |
| `correlation_id` | string | No | From `X-Request-ID` header |
| `metadata` | object | No | Extra context (tail count, dry_run, etc.) |

### Storage

`data/ops-controller/audit.log` — JSONL, append-only. Rotate at 10MB (`AUDIT_LOG_MAX_BYTES`). Export: `GET /audit?limit=N&since=ISO8601`.

### Correlation ID Flow

1. External client sends `X-Request-ID: req-abc` to model gateway
2. Model gateway logs it; includes in throughput record to dashboard
3. Dashboard passes `X-Request-ID` when calling ops controller
4. Ops controller includes in audit entry
5. Result: one request traceable across model → throughput → ops → audit

## Design Principle

**Recovery, not hot path.** Normal model and tool traffic flows OpenClaw → model gateway and OpenClaw → MCP gateway directly. Dashboard observes and administers. Ops controller restarts services, surfaces logs, coordinates upgrades. No user request should require ops-controller success to complete a chat or tool call.

## Known Limitations

- `actor` field in `_audit()` hardcoded to `"dashboard"` — acceptable for now; multi-actor needs identity propagation
- No CSRF token — sufficient for localhost deployment

## Non-Goals

- Being in the hot path for chat/tool requests
- Direct UI — all interactions go through the dashboard

## Dependencies

- Docker socket (`/var/run/docker.sock`)
- `OPS_CONTROLLER_TOKEN` from `.env`
- `ALLOWED_SERVICES` allowlist in code
