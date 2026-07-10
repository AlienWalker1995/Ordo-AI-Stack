# Ops Controller

> ⚠️ **LEGACY — superseded by the V2 `ops-api` control plane ([`v2/docker/ops-api/`](../v2/docker/ops-api/)).** Since the 2026-07-09 cutover, the production control plane is `ordo serve` = `ordo-v2/ops-controller:latest`, built from `v2/docker/ops-api/` (its Dockerfile `COPY main.py` from that context). This root `ops-controller/` directory is **unused legacy V1 code** and is flagged for deletion in a separate cleanup PR — see [`docs/LEGACY-CLEANUP.md`](../docs/LEGACY-CLEANUP.md). The V2 control plane adds a scheduler-driven `/status`, `/model-config` (drift-safe model switch), and `/jobs` (GPU reservation); the endpoints below describe the retired V1 controller.

Secure Docker Compose control plane. Exposes an authenticated API for start/stop/restart, logs, and image pulls. Dashboard calls this service; it never mounts docker.sock.

**Status:** See the [Product Requirements Docs](../docs/product%20requirements%20docs/index.md) for design and decisions.

## Endpoints

- `GET /health` — Controller health
- `GET /services` — List compose services + status
- `POST /services/{id}/start|stop|restart` — Service lifecycle (requires `confirm: true`)
- `GET /services/{id}/logs` — Tail logs
- `POST /images/pull` — Pull images for services
- `GET /audit` — Audit log

## Auth

Bearer token via `OPS_CONTROLLER_TOKEN`. Generate: `openssl rand -hex 32`.

## Security

- Never expose controller port to the public internet
- Token required for all mutating operations
- Audit log records admin actions
