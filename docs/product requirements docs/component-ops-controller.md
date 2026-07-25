# Component: Ops Controller

## Purpose

The V2 control plane (`ordo serve`, `ordo/control.py`). Drives the GPU/job broker and
scheduler, and performs the drift-safe model switch (writes the declarative `ordo.yaml`
source, then re-renders `.env` + compose + Hermes ctx in one pass so they can never
disagree). It holds `docker.sock` only for the broker's `start`/`stop` calls, and the
`DockerBackend` guard scopes every one of those to the `<project>-*` prefix — it cannot
reach containers outside this compose project.

This is **not** the audited, Bearer-token-gated compose-lifecycle API — that is a
separate, optional service, `ops-api`. See "Related service: ops-api" below.

## API Reference

**Base URL:** `http://ops-controller:9000` (internal network; no host port)

**Auth:** None. This is the agreed model (`ordo/control.py`): the dashboard is
localhost-only / reached only through the Caddy edge, and auth is the edge's job, not
baked into every internal service.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health`, `/healthz` | GET | Liveness |
| `/status` | GET | GPU/scheduler state + the current rendered manifest |
| `/model-config` | GET | Source model, resolved active model, tier, ctx size, catalog |
| `/model-config` | POST | Switch active model (`{"model": "<id>"|"auto"}`); rewrites `ordo.yaml` and re-renders |
| `/jobs` | POST | Request GPU capacity for a job (`id`, `vram_gb`) |
| `/jobs/complete` | POST | Release a completed job (`id`) |
| `/jobs/heartbeat` | POST | Heartbeat a running job (`id`) |
| `/jobs/history` | GET | Last 100 finished leases, newest first |
| `/jobs/cloud-routed` | GET | Return-and-drain jobs the scheduler routed to cloud fallback |

## Design Principle

**Recovery, not hot path.** Normal model and tool traffic flows agent clients → model
gateway and agent clients → MCP gateway directly. Ops controller only arbitrates GPU
capacity (the broker/scheduler) and performs model switches; no user request should
require ops-controller success to complete a chat or tool call.

## Non-Goals

- Being in the hot path for chat/tool requests
- Direct UI — all interactions go through the dashboard or the scheduler's own clients
- Full compose lifecycle (start/stop/restart of arbitrary services, image pulls, log
  tailing) — that is `ops-api`, see below

## Dependencies

- Docker socket (`/var/run/docker.sock`) — broker `start`/`stop` only, guard-scoped to `<project>-*`
- Rendered config dir mounted read-write at `/config` (source `ordo.yaml` + rendered `out/`) — the single write path for a model switch

## Related service: ops-api

The V1-parity dashboard's optional backend (`services/v1-parity/dashboard.yaml`,
`services/ops-api/main.py`), rendered as its own compose service named `ops-api` — not
part of ops-controller. It owns the audited, Bearer-token-gated compose-lifecycle API:

**Base URL:** `http://ops-api:9000` (internal network; no host port)

**Auth:** `Authorization: Bearer <OPS_CONTROLLER_TOKEN>` (env var name is legacy from
V1; the token gates `ops-api`, not `ops-controller`)

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/health` | GET | None | Docker daemon reachability |
| `/services` | GET | None | List compose services + state |
| `/services/{id}/start` | POST | Bearer | Start (confirm: true required) |
| `/services/{id}/stop` | POST | Bearer | Stop (confirm: true required) |
| `/services/{id}/restart` | POST | Bearer | Restart (confirm: true required) |
| `/services/{id}/logs` | GET | Bearer | Tail logs (tail=100 max 500) |
| `/images/pull` | POST | Bearer | Pull images for services |
| `/mcp/containers` | GET | Bearer | List MCP server containers |
| `/audit` | GET | Bearer | Audit log (limit=50) |

**Safety:** All mutating endpoints require `{"confirm": true}`. Optional `{"dry_run": true}` returns planned action without executing. Service targets are restricted to an `ALLOWED_SERVICES` allowlist in `services/ops-api/main.py`. Whole-stack `/compose/*` mutations stay disabled by default (`OPS_COMPOSE_MUTATIONS_ENABLED=0`) — V2's `ordo serve` (ops-controller) owns stack lifecycle.

### Audit Event Pipeline (ops-api)

#### Schema

```json
{
  "ts": "2026-03-01T12:34:56.789Z",
  "action": "restart",
  "resource": "llamacpp",
  "actor": "dashboard",
  "result": "ok",
  "detail": "",
  "correlation_id": "req-abc123",
  "metadata": {"dry_run": false}
}
```

#### Fields

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

#### Storage

`data/ops-controller/audit.log` on the host (staged V2 data tree; mounted into the
`ops-api` container at `/data`, `AUDIT_LOG_PATH=/data/audit.log`) — JSONL, append-only.
Rotate at 10MB (`AUDIT_LOG_MAX_BYTES`). Export: `GET /audit?limit=N`.

#### Correlation ID Flow

1. External client sends `X-Request-ID: req-abc` to model gateway
2. Model gateway logs it; includes in throughput record to dashboard
3. Dashboard passes `X-Request-ID` when calling `ops-api`
4. `ops-api` includes it in the audit entry
5. Result: one request traceable across model → throughput → ops-api → audit

### Known Limitations (ops-api)

- `actor` field in `_audit()` hardcoded to `"dashboard"` — acceptable for now; multi-actor needs identity propagation
- No CSRF token — sufficient for localhost deployment
